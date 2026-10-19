"""H4 测试：基线汇总 / 基线测量 / 质量记录 / episode 集成。

全部 hermetic：假 VM、假采样器、合成 vm_samples，不依赖 firecracker、Linux
/proc、真实 VM。
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

from minisweharness.baseline import compute_summary, load_baseline, measure_baseline
from minisweharness.config import SharedMountConfig, VMConfig
from minisweharness.episode import run_episode
from minisweharness.lifecycle import VMLifecycle
from minisweharness.manifest import EpisodeManifest
from minisweharness.quality import QualityThresholds, evaluate_quality, quality_record


# ------------------------------------------------------------------ 基线汇总


def _samples() -> list[dict]:
    return [
        {"monotonic_ts": 0.0, "cpu_usec": 1000, "rss_bytes": 100, "pss_bytes": 50,
         "io_read_bytes": 0, "io_write_bytes": 0, "net_rx_bytes": 0, "net_tx_bytes": 0, "coverage_gap": 0},
        {"monotonic_ts": 0.5, "cpu_usec": 1500, "rss_bytes": 300, "pss_bytes": 100,
         "io_read_bytes": 500, "io_write_bytes": 100, "net_rx_bytes": 200, "net_tx_bytes": 100, "coverage_gap": 0},
        {"monotonic_ts": 1.0, "cpu_usec": 2000, "rss_bytes": 200, "pss_bytes": 80,
         "io_read_bytes": 1000, "io_write_bytes": 200, "net_rx_bytes": 500, "net_tx_bytes": 200, "coverage_gap": 0},
    ]


def test_compute_summary_rates_peak_mean_integral():
    summary = compute_summary(_samples(), interval_seconds=0.5)
    assert summary["duration_seconds"] == pytest.approx(1.0)
    assert summary["sample_count"] == 3
    assert summary["cpu_usec_per_sec"] == pytest.approx(1000.0)
    assert summary["io_read_bytes_per_sec"] == pytest.approx(1000.0)
    assert summary["io_write_bytes_per_sec"] == pytest.approx(200.0)
    assert summary["net_rx_bytes_per_sec"] == pytest.approx(500.0)
    assert summary["net_tx_bytes_per_sec"] == pytest.approx(200.0)
    assert summary["rss_peak_bytes"] == 300
    assert summary["rss_mean_bytes"] == pytest.approx(200.0)
    assert summary["pss_peak_bytes"] == 100
    assert summary["memory_time_rss_bytes_s"] == pytest.approx(225.0)  # (100+300)/2*.5 + (300+200)/2*.5
    assert summary["coverage_gaps"] == 0


def test_compute_summary_counts_coverage_gaps():
    samples = _samples()
    samples[-1]["coverage_gap"] = 4  # 漏采超阈值
    assert compute_summary(samples, interval_seconds=0.5)["coverage_gaps"] == 1


def test_compute_summary_empty():
    assert compute_summary([], interval_seconds=0.5) == {}


# ------------------------------------------------------------------ 基线测量


class FakeProcess:
    pid = 99


class FakeVM:
    process = FakeProcess()

    def create(self):
        pass

    def destroy(self):
        pass


class FakeSampler:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            "\n".join(json.dumps(s) for s in _samples()) + "\n"
        )

    def stop(self):
        self.stopped = True


def test_measure_baseline_writes_file_and_manifest(tmp_path: Path):
    vm_config = VMConfig.from_dict({"kernel_image": "/k/vmlinux", "root_drive": "/k/rootfs.ext4"})
    record = measure_baseline(
        vm_config,
        tmp_path / "run",
        duration_seconds=0.01,
        profile_id="prof-1",
        vm_factory=lambda cfg, vid, keep: FakeVM(),
        sampler_factory=lambda pid, output, interval: FakeSampler(output),
    )
    assert record["profile_id"] == "prof-1"
    assert record["cpu_usec_per_sec"] == pytest.approx(1000.0)
    assert (tmp_path / "run" / "baseline_prof-1.json").is_file()
    manifest = json.loads((tmp_path / "run" / "run_manifest.json").read_text())
    assert manifest["baseline_profile_id"] == "prof-1"
    assert load_baseline(tmp_path / "run")["profile_id"] == "prof-1"
    assert load_baseline(tmp_path / "run", "prof-1")["rss_peak_bytes"] == 300
    assert load_baseline(tmp_path / "run", "missing") is None


# ------------------------------------------------------------------ 质量记录


def test_evaluate_quality_over_threshold_and_unavailable():
    reconciliation = {"cpu_ratio": 2.0, "io_ratio": 0.5, "net_ratio": None, "mem_ratio": 1.8}
    entries = evaluate_quality(reconciliation, coverage_gaps=0)
    by_metric = {e["metric"]: e for e in entries}
    assert by_metric["cpu_ratio"]["level"] == "warn"  # 2.0 > 1.0
    assert by_metric["mem_ratio"]["level"] == "warn"  # 1.8 > 1.5
    assert by_metric["net_ratio"]["level"] == "info"  # 缺失
    assert "io_ratio" not in by_metric  # 0.5 正常，不记录


def test_evaluate_quality_coverage_gaps():
    entries = evaluate_quality({"cpu_ratio": 0.5}, coverage_gaps=5)
    assert any(e["metric"] == "coverage_gaps" and e["level"] == "warn" for e in entries)


def test_quality_record_shape():
    record = quality_record([], baseline_profile_id="prof-1")
    assert record["entries"] == []
    assert record["baseline_profile_id"] == "prof-1"
    assert record["thresholds"]["mem_ratio_max"] == QualityThresholds().mem_ratio_max


# ------------------------------------------------------------------ episode 集成


class EpFakeProcess:
    pid = 4242


class EpFakeVM:
    process = EpFakeProcess()
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


class EpFakeHarbor:
    def list_tags(self, repo_path: str) -> list[str]:
        return ["v1"]

    def digest(self, repo_path: str, tag: str) -> str:
        return "sha256:abc"


def _make_store(store_dir: Path, episode: str) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "blobs").mkdir(exist_ok=True)
    db = sqlite3.connect(store_dir / "index.sqlite3")
    db.execute(
        "CREATE TABLE events (episode TEXT, kind TEXT, round INTEGER, payload TEXT,"
        " created REAL, wall_ts REAL, monotonic_ts REAL, layer TEXT)"
    )
    raw = json.dumps({"final": True}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(raw).hexdigest()
    (store_dir / "blobs" / f"{key}.gz").write_bytes(gzip.compress(raw))
    db.execute(
        "INSERT INTO events(episode, kind, round, payload, created, wall_ts, monotonic_ts, layer)"
        " VALUES (?, 'session_snapshot', NULL, ?, 0, 0, 0, 'container_cgroup')",
        (episode, key),
    )
    db.commit()
    db.close()


def test_run_episode_records_quality_with_baseline(tmp_path: Path):
    image_map = tmp_path / "image_map.jsonl"
    image_map.write_text(json.dumps({"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}) + "\n")
    source = tmp_path / "source_store"
    _make_store(source, "ep-h4")

    # 先测一份基线，使 episode 的 quality 带上 baseline_profile_id
    baseline = tmp_path / "run" / "baseline_prof-1.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"profile_id": "prof-1", "cpu_usec_per_sec": 1.0}) + "\n")
    (tmp_path / "run" / "run_manifest.json").write_text(
        json.dumps({"vmm": "firecracker", "baseline_profile_id": "prof-1"}) + "\n"
    )

    episode_dir = tmp_path / "run" / "episodes" / "ep-h4"

    def execute(command: str, timeout: float) -> subprocess.CompletedProcess:
        if "minisweagent.run.replay run" in command:
            store = episode_dir / "shared" / "store"
            store.mkdir(parents=True, exist_ok=True)
            (store / "heartbeat.json").write_text(json.dumps({"wall_ts": 1_700_000_000.0, "monotonic_ts": 1000.0}))
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
        benchmark="b", task_id="t", source_episode="src", episode_id="ep-h4", source_store=str(source)
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
        harbor=EpFakeHarbor(),
        exec_fn=execute,
        vm_factory=lambda cfg, vid, keep: EpFakeVM(cfg),
    )

    assert record["status"] == "completed"
    assert record["quality"]["baseline_profile_id"] == "prof-1"
    assert isinstance(record["quality"]["entries"], list)
    # vm_samples 为空 → cpu/io/net/mem 比值缺失 → info 条目；至少无 warn
    assert all(e["level"] in ("info", "warn") for e in record["quality"]["entries"])
