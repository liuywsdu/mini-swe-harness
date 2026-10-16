"""H1 采样器测试。

- 伪造 proc 树 + 注入时钟：hermetic，任何平台可跑，覆盖「已知负载下界
  断言」的静态版本（counter 注入已知增量 → 窗口差分 ≥ 增量）；
- stress-ng 真机验收（Linux + /proc + stress-ng）单独标记，默认跳过。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from minisweharness.analysis import coverage_gaps, memory_time, window_deltas
from minisweharness.sampler import (
    DEFAULT_INTERVAL_SECONDS,
    _PAGE_SIZE,
    VMMSampler,
    VMMProcessGone,
    parse_net_dev,
    parse_smaps_rollup,
    parse_stat,
    process_tree,
)
from minisweharness.timestamps import DualTimestamp

CLK_TCK = 100
PAGE_SIZE = _PAGE_SIZE  # 采样主机的页大小（真机 Linux 为 4096）


def _stat(pid: int, ppid: int, utime: int, stime: int, rss_pages: int) -> str:
    """构造 /proc/<pid>/stat 一行：pid (comm) state ppid ... utime stime ... rss。"""
    # 整行 0-based 下标：state=2 ppid=3 utime=13 stime=14 rss=23（proc(5) stat 第 4/14/15/24 列）
    fields = [str(pid), f"(p{pid})", "S"] + ["0"] * 21
    fields[3] = str(ppid)
    fields[13] = str(utime)
    fields[14] = str(stime)
    fields[23] = str(rss_pages)
    return " ".join(fields) + "\n"


NET_DEV = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
    "    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0\n"
    "  tap0: 100000 100 0 0 0 0 0 0 200000 200 0 0 0 0 0 0\n"
)


def _make_proc_tree(root: Path) -> Path:
    """root(100) → child(101)；200 是无关进程；另备 net/dev。"""
    (root / "net").mkdir(parents=True)
    (root / "net" / "dev").write_text(NET_DEV)
    stats = {
        100: _stat(100, 1, utime=1000, stime=2000, rss_pages=100),  # 3000 ticks
        101: _stat(101, 100, utime=500, stime=500, rss_pages=50),  # 1000 ticks
        200: _stat(200, 1, utime=9000, stime=9000, rss_pages=900),  # 无关进程
    }
    ios = {
        100: "read_bytes: 4096\nwrite_bytes: 8192\n",
        101: "read_bytes: 1024\nwrite_bytes: 2048\n",
    }
    smaps = {100: "Rss:  400 kB\nPss:  300 kB\n", 101: "Rss:  200 kB\nPss:  100 kB\n"}
    for pid, stat in stats.items():
        pid_dir = root / str(pid)
        pid_dir.mkdir()
        (pid_dir / "stat").write_text(stat)
        (pid_dir / "io").write_text(ios[pid])
        (pid_dir / "smaps_rollup").write_text(smaps[pid])
    return root


@pytest.fixture()
def proc_tree(tmp_path: Path) -> Path:
    return _make_proc_tree(tmp_path / "proc")


class FakeClock:
    """每次调用前进一个采样周期，模拟严格 500ms 节拍。"""

    def __init__(self, interval: float = DEFAULT_INTERVAL_SECONDS):
        self.t = 0.0
        self.interval = interval

    def __call__(self) -> DualTimestamp:
        self.t += self.interval
        return DualTimestamp(wall=1_700_000_000.0 + self.t, monotonic=self.t)


# ------------------------------------------------------------------ 解析


def test_parse_stat_handles_comm_with_spaces_and_parens():
    parsed = parse_stat("123 (fire (cracker)) S 1 0 0 0 0 0 0 0 0 0 7 11 0 0 20 0 1 0 5 0 42\n")
    assert parsed == {"ppid": 1, "utime_ticks": 7, "stime_ticks": 11, "rss_pages": 42}


def test_parse_smaps_rollup_kilobytes_to_bytes():
    assert parse_smaps_rollup("Rss:  400 kB\nPss:  300 kB\n") == {"rss_bytes": 409600, "pss_bytes": 307200}


def test_parse_net_dev_picks_iface_counters():
    assert parse_net_dev(NET_DEV, "tap0") == (100000, 200000)
    assert parse_net_dev(NET_DEV, "eth9") is None


# ------------------------------------------------------------------ 进程树


def test_process_tree_includes_descendants_only(proc_tree: Path):
    assert process_tree(100, proc_tree) == {100, 101}
    assert process_tree(200, proc_tree) == {200}
    assert process_tree(999, proc_tree) == set()


# ------------------------------------------------------------------ 采样


def _sampler(proc_tree: Path, clock: FakeClock, output: str = "/dev/null", **kw) -> VMMSampler:
    return VMMSampler(100, output, tap_device="tap0", proc_root=proc_tree, clock=clock, **kw)


def test_sample_once_aggregates_tree(proc_tree: Path):
    sample = _sampler(proc_tree, FakeClock()).sample_once()
    assert sample["layer"] == "host_vmm"
    assert sample["vmm_pids"] == [100, 101]
    assert sample["cpu_usec"] == round(4000 * 1e6 / CLK_TCK)
    assert sample["rss_bytes"] == 150 * PAGE_SIZE
    assert sample["pss_bytes"] == 400 * 1024
    assert sample["io_read_bytes"] == 4096 + 1024
    assert sample["io_write_bytes"] == 8192 + 2048
    assert sample["net_rx_bytes"] == 100000
    assert sample["net_tx_bytes"] == 200000
    assert sample["coverage_gap"] == 0  # 首个样本无前置窗口


def test_sample_once_nulls_when_unsupported(tmp_path: Path):
    proc = _make_proc_tree(tmp_path / "proc")
    (proc / "net" / "dev").write_text("  lo: 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n")  # 无 tap0
    (proc / "100" / "smaps_rollup").unlink()  # 模拟无权限读
    sampler = _sampler(proc, FakeClock())
    sample = sampler.sample_once()
    assert sample["net_rx_bytes"] is None  # 不支持记 null（横向前提③）
    assert sample["pss_bytes"] is None  # 任一进程缺 pss → 整体 null
    assert sampler.errors


def test_known_load_lower_bound_assertions(proc_tree: Path):
    """静态版验收：注入已知增量（模拟 stress-ng 负载）→ 窗口差分 ≥ 增量。"""
    clock = FakeClock()
    sampler = _sampler(proc_tree, clock)
    first = sampler.sample_once()

    (proc_tree / "100" / "stat").write_text(
        _stat(100, 1, utime=1000 + 2 * CLK_TCK, stime=2000, rss_pages=200)
    )
    (proc_tree / "100" / "io").write_text(
        f"read_bytes: {4096 + 3 * 2**20}\nwrite_bytes: {8192 + 10 * 2**20}\n"
    )
    (proc_tree / "net" / "dev").write_text(
        NET_DEV.replace("100000 100", f"{100000 + 5 * 2**20} 100", 1)
    )
    second = sampler.sample_once()

    delta = window_deltas([first, second])[0]
    assert delta["window_seconds"] == pytest.approx(DEFAULT_INTERVAL_SECONDS)
    assert delta["cpu_usec"] >= 2_000_000  # +2s CPU
    assert delta["io_read_bytes"] >= 3 * 2**20  # +3MiB 读
    assert delta["io_write_bytes"] >= 10 * 2**20  # +10MiB 写
    assert delta["net_rx_bytes"] >= 5 * 2**20  # +5MiB rx
    assert second["coverage_gap"] == 0


def test_coverage_gap_flagged_after_missed_cycles(proc_tree: Path):
    clock = FakeClock()
    sampler = _sampler(proc_tree, clock)
    sampler.sample_once()
    # 已采 1 样本（t=0.5），再手动前进 4 个周期后采样：elapsed=2.5s → 漏采 4 个
    clock.t += 4 * DEFAULT_INTERVAL_SECONDS
    late = sampler.sample_once()
    assert late["coverage_gap"] == 4
    assert coverage_gaps([late], limit=3) == [late]


def test_root_gone_raises(proc_tree: Path):
    sampler = _sampler(proc_tree, FakeClock())
    for pid_dir in list(proc_tree.iterdir()):
        if pid_dir.name.isdigit():
            shutil.rmtree(pid_dir)
    with pytest.raises(VMMProcessGone):
        sampler.sample_once()


def test_run_loop_writes_jsonl(proc_tree: Path, tmp_path: Path):
    output = tmp_path / "vm_samples.jsonl"
    sampler = _sampler(proc_tree, FakeClock(interval=0.01), output=str(output), interval_seconds=0.01)
    sampler.start()
    time.sleep(0.06)
    sampler.stop()
    samples = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(samples) >= 2
    assert all(s["vmm_pids"] == [100, 101] for s in samples)
    assert all(s["layer"] == "host_vmm" for s in samples)


# ------------------------------------------------------------------ 积分


def test_memory_time_trapezoid():
    samples = [
        {"monotonic_ts": 0.0, "rss_bytes": 100},
        {"monotonic_ts": 0.5, "rss_bytes": 300},
        {"monotonic_ts": 1.0, "rss_bytes": 200},
    ]
    # (100+300)/2*0.5 + (300+200)/2*0.5 = 100 + 125 = 225 byte·s
    assert memory_time(samples) == pytest.approx(225.0)


def test_memory_time_null_when_missing():
    samples = [{"monotonic_ts": 0.0, "rss_bytes": None}, {"monotonic_ts": 0.5, "rss_bytes": 100}]
    assert memory_time(samples) is None


# ---------------------------------------------------- 真机验收（Linux only）


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires Linux /proc")
@pytest.mark.skipif(shutil.which("stress-ng") is None, reason="requires stress-ng")
def test_stress_ng_lower_bounds_host(tmp_path: Path):
    """H1 真机验收：stress-ng 已知负载下 CPU/内存/IO 下界断言。"""
    proc = tmp_path / "proc"
    proc.symlink_to("/proc")
    victim = subprocess.Popen(
        ["stress-ng", "--cpu", "1", "--vm", "1", "--vm-bytes", "128m", "--io", "1", "--hdd", "1", "--timeout", "4s"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    sampler = VMMSampler(victim.pid, tmp_path / "vm_samples.jsonl", interval_seconds=0.2, proc_root=proc)
    samples = []
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            samples.append(sampler.sample_once())
            time.sleep(0.2)
    finally:
        victim.wait(timeout=10)
    assert len(samples) >= 5
    assert not coverage_gaps(samples)
    deltas = window_deltas(samples)
    busy = [d for d in deltas if d["cpu_usec"] is not None]
    assert max(d["cpu_usec"] for d in busy) > 0.3 * 1e6  # 单窗口 CPU ≥ 0.3s/s
    assert max(s["rss_bytes"] for s in samples) >= 128 * 2**20 * 0.8  # vm 驻留下界
    assert sum(d["io_write_bytes"] for d in busy) > 2**20  # hdd 累计写下界 1MiB
