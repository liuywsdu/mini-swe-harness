"""VMM 采样器：按 VMM 进程树读 ``/proc``，500ms 周期产出 ``vm_samples.jsonl``。

对应计划文档 §5.1「VMM 采样器」（改造 ``gpu_tasks/container.py:watch()``
思路）与 §6 ``vm_samples.jsonl`` 输出规范。

口径：
- ``cpu_usec`` / ``io_*``：累计计数器（相邻样本差分得窗口消耗）；
- ``rss_bytes`` / ``pss_bytes``：瞬时值（memory-time 由样本梯形积分，
  见 ``minisweharness.analysis``）；
- ``net_*``：tap 设备 ``/proc/net/dev`` 累计字节；
- ``coverage_gap``：距上个样本漏采的周期数（>3 视为 coverage 事故）；
- 不支持读（如非 Linux、无权限）记 null（横向前提③）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from minisweharness.timestamps import DualTimestamp, now

DEFAULT_INTERVAL_SECONDS = 0.5
SAMPLE_LAYER = "host_vmm"
COVERAGE_GAP_LIMIT = 3

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (ValueError, OSError, AttributeError):
    _PAGE_SIZE = 4096


class VMMProcessGone(ProcessLookupError):
    """VMM 根进程已退出，采样无法继续。"""


# --------------------------------------------------------------------- 解析


def parse_stat(text: str) -> dict:
    """解析 ``/proc/<pid>/stat``；comm 可能含空格/括号，从最后一个 ')' 切分。"""
    after_comm = text[text.rfind(")") + 2 :].split()
    # after_comm[0]=state [1]=ppid [11]=utime [12]=stime [21]=rss
    return {
        "ppid": int(after_comm[1]),
        "utime_ticks": int(after_comm[11]),
        "stime_ticks": int(after_comm[12]),
        "rss_pages": int(after_comm[21]),
    }


def parse_io(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, _, raw = line.partition(":")
        key = key.strip()
        if key in ("read_bytes", "write_bytes"):
            values[key] = int(raw.strip())
    return values


def parse_smaps_rollup(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, _, raw = line.partition(":")
        key = key.strip()
        if key in ("Rss", "Pss"):
            values[key.lower() + "_bytes"] = int(raw.strip().split()[0]) * 1024
    return values


def parse_net_dev(text: str, iface: str) -> tuple[int, int] | None:
    """从 ``/proc/net/dev`` 读指定接口的累计 rx/tx 字节。"""
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name.strip() != iface:
            continue
        fields = rest.split()
        return int(fields[0]), int(fields[8])  # rx_bytes, tx_bytes
    return None


# ------------------------------------------------------------------- 进程树


def _read_ppid(pid: int, proc_root: Path) -> int | None:
    try:
        return parse_stat((proc_root / str(pid) / "stat").read_text())["ppid"]
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
        return None


def process_tree(root_pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    """root_pid 及其全部后代 pid（容忍采样中途进程消失）。"""
    ppids: dict[int, int | None] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppids[pid] = _read_ppid(pid, proc_root)
    if ppids.get(root_pid) is None:
        return set()  # 根进程不可读：整棵树视为已退出
    tree = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in ppids.items():
            if pid not in tree and ppid in tree:
                tree.add(pid)
                changed = True
    return tree


# --------------------------------------------------------------------- 采样


class VMMSampler:
    """对一棵 VMM 进程树周期采样，写 JSONL。

    ``proc_root`` 与 ``clock`` 可注入：测试用伪造 proc 树与固定时钟，
    真机用默认 ``/proc`` 与真实双时钟。
    """

    def __init__(
        self,
        root_pid: int,
        output_path: str | Path,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        tap_device: str | None = None,
        extra_pids: Iterable[int] = (),
        proc_root: Path = Path("/proc"),
        clock: Callable[[], DualTimestamp] = now,
    ):
        self.root_pid = root_pid
        self.output_path = Path(output_path)
        self.interval_seconds = interval_seconds
        self.tap_device = tap_device
        self.extra_pids = tuple(extra_pids)
        self.proc_root = Path(proc_root)
        self.clock = clock
        self._last_monotonic: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []

    # ---------------------------------------------------------------- 单次

    def sample_once(self) -> dict:
        timestamp = self.clock()
        pids = self._tree_pids()
        if not pids:
            raise VMMProcessGone(f"VMM process tree empty (root pid {self.root_pid} gone?)")

        cpu_usec = rss_bytes = io_read = io_write = 0
        pss_parts: list[int] = []
        live_pids: list[int] = []
        for pid in sorted(pids):
            pid_dir = self.proc_root / str(pid)
            try:
                stat = parse_stat((pid_dir / "stat").read_text())
            except (FileNotFoundError, ProcessLookupError):
                continue  # 子进程消失：容忍，根进程由 _tree_pids 兜底
            live_pids.append(pid)
            cpu_usec += round((stat["utime_ticks"] + stat["stime_ticks"]) * 1e6 / _CLK_TCK)
            rss_bytes += stat["rss_pages"] * _PAGE_SIZE
            io_parts = self._read_optional(pid_dir / "io", parse_io) or {}
            io_read += io_parts.get("read_bytes", 0)
            io_write += io_parts.get("write_bytes", 0)
            smaps = self._read_optional(pid_dir / "smaps_rollup", parse_smaps_rollup) or {}
            if "pss_bytes" in smaps:
                pss_parts.append(smaps["pss_bytes"])

        net_rx = net_tx = None
        if self.tap_device:
            net = self._read_optional(self.proc_root / "net" / "dev", lambda t: parse_net_dev(t, self.tap_device))
            if net is not None:
                net_rx, net_tx = net

        gap = 0
        if self._last_monotonic is not None:
            elapsed = timestamp.monotonic - self._last_monotonic
            gap = max(0, int(elapsed / self.interval_seconds) - 1)
        self._last_monotonic = timestamp.monotonic

        return {
            "wall_ts": timestamp.wall,
            "monotonic_ts": timestamp.monotonic,
            "layer": SAMPLE_LAYER,
            "cpu_usec": cpu_usec,
            "rss_bytes": rss_bytes,
            "pss_bytes": sum(pss_parts) if len(pss_parts) == len(live_pids) and live_pids else None,
            "io_read_bytes": io_read,
            "io_write_bytes": io_write,
            "net_rx_bytes": net_rx,
            "net_tx_bytes": net_tx,
            "vmm_pids": live_pids,
            "coverage_gap": gap,
        }

    def _tree_pids(self) -> set[int]:
        try:
            tree = process_tree(self.root_pid, self.proc_root)
        except FileNotFoundError:
            return set()
        return tree | {p for p in self.extra_pids if (self.proc_root / str(p)).exists()}

    def _read_optional(self, path: Path, parser: Callable[[str], object]) -> object | None:
        """读取并解析；单点失败（消失/无权限/格式异常）记 errors 后返回 None。"""
        try:
            return parser(path.read_text())
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError) as exc:
            self.errors.append(f"{type(exc).__name__}: {path}")
            return None

    # ---------------------------------------------------------------- 循环

    def run(self) -> None:
        """周期采样直到 ``stop()``（watch() 同款循环结构）。"""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a") as stream:
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    stream.write(json.dumps(self.sample_once(), ensure_ascii=False) + "\n")
                    stream.flush()
                except VMMProcessGone:
                    return
                except Exception as exc:  # noqa: BLE001 — 单点故障不拖垮采样
                    self.errors.append(f"sample failed: {type(exc).__name__}: {exc}")
                overdue = time.monotonic() - started
                self._stop_event.wait(max(0.0, self.interval_seconds - overdue))

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name=f"vmm-sampler-{self.root_pid}", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def __enter__(self) -> "VMMSampler":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
