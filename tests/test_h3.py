"""H3 测试：时钟探针 / 双层 join / 对账 / episode 集成。

全部 hermetic：不依赖 firecracker、docker、真实 Linux /proc、真实 guest
心跳。episode 集成测试注入假 VM、假 exec（写 heartbeat 与 guest_events）、
假 Harbor。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from minisweharness.clocksync import ClockProbe, compute_offset
from minisweharness.config import SharedMountConfig, VMConfig
from minisweharness.episode import run_episode
from minisweharness.images import DEFAULT_REGISTRY
from minisweharness.join import (
    guest_totals,
    host_totals,
    load_jsonl,
    map_guest_to_host,
    reconcile,
)
from minisweharness.lifecycle import VMLifecycle
from minisweharness.manifest import EpisodeManifest
from minisweharness.timestamps import DualTimestamp


# ------------------------------------------------------------------ 时钟探针


def test_compute_offset_median_and_drift():
    samples = [
        {"guest_wall": 100.0, "guest_monotonic": 10.0, "host_wall": 99.0, "host_monotonic": 0.0},
        {"guest_wall": 101.0, "guest_monotonic": 11.0, "host_wall": 100.0, "host_monotonic": 1.0},
        {"guest_wall": 102.0, "guest_monotonic": 12.0, "host_wall": 101.0, "host_monotonic": 2.0},
    ]
    sync = compute_offset(samples)
    assert sync["probe_count"] == 3
    assert sync["offset_seconds"] == pytest.approx(1.0)
    assert sync["drift_ppm"] == pytest.approx(0.0, abs=1e-9)  # 无漂移


def test_compute_offset_drift_detected():
    # guest 时钟每 host 秒多走 1e-4 秒 → 100 ppm
    samples = [
        {"guest_wall": 10.0, "guest_monotonic": 1.0, "host_wall": 9.0, "host_monotonic": 0.0},
        {"guest_wall": 11.0001, "guest_monotonic": 2.0, "host_wall": 10.0, "host_monotonic": 1.0},
    ]
    sync = compute_offset(samples)
    assert sync["drift_ppm"] == pytest.approx(100.0, rel=1e-3)


def test_compute_offset_empty_and_single():
    assert compute_offset([]) == {"offset_seconds": None, "drift_ppm": None, "probe_count": 0}
    single = [{"guest_wall": 5.0, "guest_monotonic": 1.0, "host_wall": 4.0, "host_monotonic": 0.0}]
    assert compute_offset(single)["drift_ppm"] is None
    assert compute_offset(single)["offset_seconds"] == pytest.approx(1.0)


def test_clock_probe_collect_once_and_dedup(tmp_path: Path):
    heartbeat = tmp_path / "heartbeat.json"
    stamps = iter([DualTimestamp(100.0, 0.0), DualTimestamp(101.0, 1.0)])
    probe = ClockProbe(heartbeat, clock=lambda: next(stamps))
    assert probe.collect_once() is None  # 文件不存在

    heartbeat.write_text(json.dumps({"episode_id": "e", "wall_ts": 50.0, "monotonic_ts": 5.0}))
    first = probe.collect_once()
    assert first == {"guest_wall": 50.0, "guest_monotonic": 5.0, "host_wall": 100.0, "host_monotonic": 0.0}
    assert probe.collect_once() is None  # guest 内容未变，去重

    heartbeat.write_text(json.dumps({"episode_id": "e", "wall_ts": 51.0, "monotonic_ts": 6.0}))
    assert probe.collect_once()["guest_wall"] == 51.0
    assert probe.calibrate()["probe_count"] == 2


# ------------------------------------------------------------------ join / 对账


def _synthetic_guest_events() -> list[dict]:
    return [
        {
            "kind": "action_timing",
            "round": 1,
            "wall_ts": 1005.0,
            "monotonic_ts": 1.0,
            "layer": "container_cgroup",
            "payload": {
                "resources": {
                    "cpu_usage_usec": 40,
                    "memory_peak_bytes": 1 * 2**20,
                    "io_read_bytes": 100,
                    "io_write_bytes": 50,
                }
            },
        },
        {
            "kind": "action_timing",
            "round": 2,
            "wall_ts": 1006.0,
            "monotonic_ts": 2.0,
            "layer": "container_cgroup",
            "payload": {
                "resources": {
                    "cpu_usage_usec": 60,
                    "memory_peak_bytes": 2 * 2**20,
                    "io_read_bytes": 200,
                    "io_write_bytes": 50,
                }
            },
        },
        {
            "kind": "session_snapshot",
            "round": None,
            "wall_ts": 1007.0,
            "monotonic_ts": 3.0,
            "layer": "container_cgroup",
            "payload": {"final": True, "network_delta": {"rx_bytes": 1000, "tx_bytes": 500}},
        },
    ]


def _synthetic_host_samples() -> list[dict]:
    return [
        {"monotonic_ts": 0.0, "cpu_usec": 1000, "rss_bytes": 4 * 2**20, "io_read_bytes": 500, "io_write_bytes": 300, "net_rx_bytes": 0, "net_tx_bytes": 0},
        {"monotonic_ts": 0.5, "cpu_usec": 1200, "rss_bytes": 4 * 2**20, "io_read_bytes": 700, "io_write_bytes": 500, "net_rx_bytes": 1000, "net_tx_bytes": 400},
        {"monotonic_ts": 1.0, "cpu_usec": 1600, "rss_bytes": 5 * 2**20, "io_read_bytes": 1200, "io_write_bytes": 700, "net_rx_bytes": 3000, "net_tx_bytes": 1000},
    ]


def test_map_guest_to_host_applies_offset():
    mapped = map_guest_to_host([{"wall_ts": 1005.0, "monotonic_ts": 1.0}], offset_seconds=5.0)
    assert mapped[0]["host_wall_ts"] == pytest.approx(1000.0)


def test_guest_totals_window_and_sums():
    totals = guest_totals(
        _synthetic_guest_events(),
        offset_seconds=5.0,
        window_start_wall=1000.0,
        window_end_wall=1003.0,
    )
    assert totals["cpu_usec"] == 100
    assert totals["io_read_bytes"] == 300
    assert totals["io_write_bytes"] == 100
    assert totals["memory_peak_bytes"] == 2 * 2**20
    assert totals["net_rx_bytes"] == 1000
    assert totals["net_tx_bytes"] == 500
    assert totals["action_count"] == 2


def test_guest_totals_window_excludes_outside_events():
    totals = guest_totals(
        _synthetic_guest_events(),
        offset_seconds=5.0,
        window_start_wall=1002.0,
        window_end_wall=1002.5,
    )
    assert totals["cpu_usec"] == 0
    assert totals["action_count"] == 0


def test_host_totals_deltas_and_peak():
    totals = host_totals(_synthetic_host_samples(), window_start_monotonic=0.0, window_end_monotonic=1.0)
    assert totals["cpu_usec"] == 600
    assert totals["io_read_bytes"] == 700
    assert totals["io_write_bytes"] == 400
    assert totals["net_rx_bytes"] == 3000
    assert totals["net_tx_bytes"] == 1000
    assert totals["rss_peak_bytes"] == 5 * 2**20
    assert totals["sample_count"] == 3


def test_reconcile_ratios_within_one():
    guest = guest_totals(
        _synthetic_guest_events(), offset_seconds=5.0, window_start_wall=1000.0, window_end_wall=1003.0
    )
    host = host_totals(_synthetic_host_samples(), window_start_monotonic=0.0, window_end_monotonic=1.0)
    result = reconcile(guest, host)
    assert 0 < result["cpu_ratio"] <= 1
    assert 0 < result["mem_ratio"] <= 1
    assert 0 < result["io_ratio"] <= 1
    assert 0 < result["net_ratio"] <= 1
    assert isinstance(result["notes"], list) and result["notes"]


def test_reconcile_nulls_when_host_missing():
    result = reconcile({"cpu_usec": 10, "memory_peak_bytes": 1}, {})
    assert result["cpu_ratio"] is None
    assert result["mem_ratio"] is None


def test_clock_probe_reads_heartbeat_through_ssh_reader():
    from minisweharness.timestamps import DualTimestamp

    payload = json.dumps({"wall_ts": 101.0, "monotonic_ts": 5.0})
    probe = ClockProbe(
        "/mnt/share/store/heartbeat.json",
        read_text_fn=lambda: payload,
        clock=lambda: DualTimestamp(wall=100.0, monotonic=10.0),
    )
    sample = probe.collect_once()
    assert sample is not None
    assert sample["guest_wall"] == 101.0
    assert sample["host_wall"] == 100.0
    assert probe.calibrate()["offset_seconds"] == 1.0


# ------------------------------------------------------------------ episode 集成


class FakeProcess:
    pid = 4242


class FakeVM:
    process = FakeProcess()
    virtiofsd_process = None

    def __init__(self, config=None):
        self.lifecycle = VMLifecycle(vm_id="fake", vmm="firecracker")
        shared = config.shared_mount if config is not None else None
        self.host_root = Path(shared.host_dir) if shared is not None else None
        self.guest_root = shared.guest_path if shared is not None else "/mnt/share"

    def create(self):
        self.lifecycle.mark("create_start")
        self.lifecycle.mark("ready")

    def destroy(self):
        self.lifecycle.mark("destroy_confirmed")

    def _local(self, guest_path):
        relative = str(guest_path).removeprefix(self.guest_root).lstrip("/")
        return self.host_root / relative

    @staticmethod
    def _copy_file(source, destination):
        source, destination = Path(source), Path(destination)
        if source.resolve() == destination.resolve():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def upload_file(self, local_path, guest_path, timeout=None):
        self._copy_file(local_path, self._local(guest_path))

    def download_file(self, guest_path, local_path, timeout=None):
        self._copy_file(self._local(guest_path), local_path)

    @staticmethod
    def _copy_dir(source, destination):
        source, destination = Path(source), Path(destination)
        if source.resolve() == destination.resolve():
            return
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)

    def upload_directory(self, local_dir, guest_dir, timeout=None):
        self._copy_dir(local_dir, self._local(guest_dir))

    def download_directory(self, guest_dir, local_dir, timeout=None):
        self._copy_dir(self._local(guest_dir), local_dir)

    def read_guest_file(self, guest_path, timeout=None):
        path = self._local(guest_path)
        return path.read_text() if path.is_file() else None


class FakeHarbor:
    def list_tags(self, repo_path: str) -> list[str]:
        return ["v1"]

    def digest(self, repo_path: str, tag: str) -> str:
        return "sha256:abc"


def _make_store(store_dir: Path, episode: str, rows: list[tuple[str, dict]]) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "blobs").mkdir(exist_ok=True)
    db = sqlite3.connect(store_dir / "index.sqlite3")
    db.execute(
        "CREATE TABLE events (episode TEXT, kind TEXT, round INTEGER, payload TEXT,"
        " created REAL, wall_ts REAL, monotonic_ts REAL, layer TEXT)"
    )
    for kind, payload in rows:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = hashlib.sha256(raw).hexdigest()
        (store_dir / "blobs" / f"{key}.gz").write_bytes(gzip.compress(raw))
        db.execute(
            "INSERT INTO events(episode, kind, round, payload, created, wall_ts, monotonic_ts, layer)"
            " VALUES (?, ?, NULL, ?, 0, 0, 0, 'container_cgroup')",
            (episode, kind, key),
        )
    db.commit()
    db.close()


def test_run_episode_writes_clock_sync_and_reconciliation(tmp_path: Path):
    image_map = tmp_path / "image_map.jsonl"
    image_map.write_text(json.dumps({"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}) + "\n")
    source = tmp_path / "source_store"
    _make_store(source, "ep-h3", [("session_snapshot", {"final": True})])

    episode_dir = tmp_path / "run" / "episodes" / "ep-h3"

    def execute(command: str, timeout: float) -> subprocess.CompletedProcess:
        if "run replay run" in command:
            store = episode_dir / "shared" / "store"
            store.mkdir(parents=True, exist_ok=True)
            (store / "heartbeat.json").write_text(
                json.dumps({"episode_id": "ep-h3", "wall_ts": 1_700_000_000.0, "monotonic_ts": 1000.0})
            )
        if "export-events" in command:
            (episode_dir / "shared" / "guest_events.jsonl").write_text(
                json.dumps(
                    {
                        "kind": "session_snapshot",
                        "round": None,
                        "wall_ts": 1_700_000_000.0,
                        "monotonic_ts": 1000.0,
                        "layer": "container_cgroup",
                        "payload": {"final": True, "network_delta": {"rx_bytes": 1, "tx_bytes": 1}},
                    }
                )
                + "\n"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    manifest = EpisodeManifest(
        benchmark="b", task_id="t", source_episode="src", episode_id="ep-h3", source_store=str(source)
    )
    vm_config = VMConfig.from_dict(
        {
            "kernel_image": "/k/vmlinux",
            "root_drive": "/k/rootfs.ext4",
            "shared_mount": SharedMountConfig(host_dir=str(tmp_path / "share")).__dict__,
        }
    )
    record = run_episode(
        manifest,
        vm_config,
        tmp_path / "run",
        image_map_path=image_map,
        harbor=FakeHarbor(),
        exec_fn=execute,
        vm_factory=lambda cfg, vid, keep: FakeVM(cfg),
    )

    assert record["status"] == "completed"
    sync = json.loads((episode_dir / "clock_sync.json").read_text())
    assert sync["probe_count"] >= 1
    assert sync["offset_seconds"] is not None
    reconciliation = json.loads((episode_dir / "reconciliation.json").read_text())
    assert set(reconciliation) == {"cpu_ratio", "mem_ratio", "io_ratio", "net_ratio", "notes"}
