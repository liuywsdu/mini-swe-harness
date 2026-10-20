"""H5 测试：guardrail 读数/接入判定 / 并行调度（并发上限、拒绝、排队、异常）。

全部 hermetic：假 snapshot、假 run_episode_fn，不启动真实 VM。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from minisweharness.config import VMConfig
from minisweharness.guardrail import (
    GuardrailPolicy,
    HostSnapshot,
    admit,
    read_host_snapshot,
)
from minisweharness.manifest import EpisodeManifest
from minisweharness.scheduler import run_episodes


def _manifest(task_id: str) -> EpisodeManifest:
    return EpisodeManifest(benchmark="b", task_id=task_id, source_episode="src", episode_id=f"ep-{task_id}")


def _vm_config() -> VMConfig:
    return VMConfig.from_dict({"kernel_image": "/k/vmlinux", "root_drive": "/k/rootfs.ext4"})


def _ok_snapshot() -> HostSnapshot:
    return HostSnapshot(available_memory_gib=100.0, disk_free_gib=100.0)


# ------------------------------------------------------------------ guardrail


def test_read_host_snapshot_parses_meminfo(tmp_path: Path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 33554432 kB\nMemAvailable: 16777216 kB\n")
    snapshot = read_host_snapshot(tmp_path, meminfo_path=meminfo)
    assert snapshot.available_memory_gib == pytest.approx(16777216 * 1024 / 2**30)
    assert snapshot.disk_free_gib is not None and snapshot.disk_free_gib > 0


def test_admit_memory_and_disk_floors():
    policy = GuardrailPolicy(
        max_concurrency=2,
        host_available_memory_floor_gib=12.0,
        host_disk_free_floor_gib=50.0,
    )
    assert admit(HostSnapshot(20.0, 80.0), policy) == (True, "")
    admitted, reason = admit(HostSnapshot(5.0, 80.0), policy)
    assert not admitted and "memory" in reason
    admitted, reason = admit(HostSnapshot(20.0, 10.0), policy)
    assert not admitted and "disk" in reason
    assert admit(HostSnapshot(None, None), policy) == (True, "")  # 读数缺失不阻断（横向前提③）


# ------------------------------------------------------------------ 并行调度


def test_run_episodes_respects_concurrency(tmp_path: Path):
    active = 0
    max_active = 0
    lock = threading.Lock()

    def run_fn(manifest: EpisodeManifest, slot_index: int) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"episode_id": manifest.resolved_episode_id, "status": "completed"}

    # 单 config = 单网络身份 = 单槽位（串行）；并发需多槽位 config 池
    results = run_episodes(
        [_manifest(str(i)) for i in range(4)],
        _slot_pool(2),
        tmp_path / "run",
        policy=GuardrailPolicy(max_concurrency=2),
        snapshot_fn=_ok_snapshot,
        run_episode_fn=run_fn,
    )
    assert len(results) == 4
    assert all(r["status"] == "completed" for r in results)
    assert max_active <= 2
    assert max_active == 2  # 确有并发


def test_run_episodes_rejects_on_pressure(tmp_path: Path):
    def run_fn(manifest: EpisodeManifest, slot_index: int) -> dict:
        raise AssertionError("should not run when guardrail rejects")

    results = run_episodes(
        [_manifest("t")],
        _vm_config(),
        tmp_path / "run",
        policy=GuardrailPolicy(queue_on_pressure=False),
        snapshot_fn=lambda: HostSnapshot(1.0, 100.0),  # 内存不足
        run_episode_fn=run_fn,
    )
    assert results[0]["status"] == "rejected"
    assert "memory" in results[0]["reject_reason"]
    episode_json = tmp_path / "run" / "episodes" / "ep-t" / "episode.json"
    assert episode_json.is_file()
    assert "rejected" in episode_json.read_text()


def test_run_episodes_queues_then_admits(tmp_path: Path):
    calls = [0]
    ran = []

    def snapshot_fn() -> HostSnapshot:
        calls[0] += 1
        return _ok_snapshot() if calls[0] > 3 else HostSnapshot(1.0, 100.0)

    def run_fn(manifest: EpisodeManifest, slot_index: int) -> dict:
        ran.append(manifest.resolved_episode_id)
        return {"episode_id": manifest.resolved_episode_id, "status": "completed"}

    results = run_episodes(
        [_manifest("t")],
        _vm_config(),
        tmp_path / "run",
        policy=GuardrailPolicy(
            queue_on_pressure=True, admission_poll_seconds=0.01, admission_timeout_seconds=5.0
        ),
        snapshot_fn=snapshot_fn,
        run_episode_fn=run_fn,
    )
    assert results[0]["status"] == "completed"
    assert ran == ["ep-t"]


def test_run_episodes_queue_timeout_rejects(tmp_path: Path):
    results = run_episodes(
        [_manifest("t")],
        _vm_config(),
        tmp_path / "run",
        policy=GuardrailPolicy(
            queue_on_pressure=True, admission_poll_seconds=0.01, admission_timeout_seconds=0.05
        ),
        snapshot_fn=lambda: HostSnapshot(1.0, 100.0),
        run_episode_fn=lambda m: (_ for _ in ()).throw(AssertionError()),
    )
    assert results[0]["status"] == "rejected"
    assert "queue timeout" in results[0]["reject_reason"]


def test_run_episodes_records_exception_from_runner(tmp_path: Path):
    def run_fn(manifest: EpisodeManifest, slot_index: int) -> dict:
        raise RuntimeError("boom")

    results = run_episodes(
        [_manifest("t")],
        _vm_config(),
        tmp_path / "run",
        policy=GuardrailPolicy(max_concurrency=1),
        snapshot_fn=_ok_snapshot,
        run_episode_fn=run_fn,
    )
    assert results[0]["status"] == "failed"
    assert "boom" in results[0]["failure"]
    episode_json = tmp_path / "run" / "episodes" / "ep-t" / "episode.json"
    assert episode_json.is_file()


# ------------------------------------------------------------------ 并发网络槽位


def _slot_pool(size: int) -> list:
    from minisweharness.config import derive_slot_config

    base = _vm_config()
    return [derive_slot_config(base, slot) for slot in range(size)]


def test_run_episodes_assigns_distinct_slots_and_configs(tmp_path: Path):
    seen: dict[int, str] = {}
    lock = threading.Lock()

    def run_fn(manifest: EpisodeManifest, slot_index: int) -> dict:
        with lock:
            seen[manifest.resolved_episode_id] = slot_index
        time.sleep(0.02)
        return {"episode_id": manifest.resolved_episode_id, "status": "completed"}

    pool = _slot_pool(2)
    results = run_episodes(
        [_manifest(str(i)) for i in range(4)],
        pool,
        tmp_path / "run",
        policy=GuardrailPolicy(max_concurrency=2),
        snapshot_fn=_ok_snapshot,
        run_episode_fn=run_fn,
    )
    assert all(r["status"] == "completed" for r in results)
    # 同一时刻槽位互不重复 → 4 个 episode 分到的槽位都在 {0,1} 且无超发
    assert set(seen.values()) <= {0, 1}
    assert len(seen) == 4


def test_run_episodes_rejects_pool_with_conflicting_configs(tmp_path: Path):
    duplicate = _vm_config()
    with pytest.raises(ValueError, match="tap_device"):
        run_episodes(
            [_manifest("t")],
            [duplicate, duplicate],
            tmp_path / "run",
            policy=GuardrailPolicy(max_concurrency=2),
            snapshot_fn=_ok_snapshot,
            run_episode_fn=lambda m, s: {"status": "completed"},
        )


def test_run_episodes_throttles_to_pool_size(tmp_path: Path):
    active = 0
    max_active = 0
    lock = threading.Lock()

    def run_fn(manifest: EpisodeManifest, slot_index: int) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"episode_id": manifest.resolved_episode_id, "status": "completed"}

    # 3 并发 policy 但 config 池只有 2 个槽位 → 同时运行不超过 2
    run_episodes(
        [_manifest(str(i)) for i in range(4)],
        _slot_pool(2),
        tmp_path / "run",
        policy=GuardrailPolicy(max_concurrency=3),
        snapshot_fn=_ok_snapshot,
        run_episode_fn=run_fn,
    )
    assert max_active == 2


# ------------------------------------------------------------------ CLI 批量 manifest


def test_run_manifest_arg_accepts_multiple_files_per_flag(tmp_path: Path):
    """--manifest 每次可空格分隔多个文件，也可多次传入；--manifest-dir 追加目录。"""
    from minisweharness.cli import _manifest_paths, build_parser

    for name in ("a.json", "b.json", "c.json"):
        (tmp_path / name).write_text("{}")
    sub = tmp_path / "sub"
    sub.mkdir()
    for name in ("d.json", "e.json"):
        (sub / name).write_text("{}")

    base = ["run", "--config", "vm.json", "--run-dir", "runs/x", "--image-map", "tasks.jsonl"]
    parser = build_parser()
    args = parser.parse_args(
        base + ["--manifest", "a.json", "b.json", "--manifest", "c.json", "--manifest-dir", str(sub)]
    )
    assert _manifest_paths(args) == ["a.json", "b.json", "c.json", str(sub / "d.json"), str(sub / "e.json")]

    # 单独 --manifest-dir 也合法（--manifest 不再 required）
    args = parser.parse_args(base + ["--manifest-dir", str(sub)])
    assert _manifest_paths(args) == [str(sub / "d.json"), str(sub / "e.json")]

    # 两者都不给 → 空列表由 _cmd_run 显式报错
    args = parser.parse_args(base)
    assert _manifest_paths(args) == []
