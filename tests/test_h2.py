"""H2 测试：manifest / 映射表 / Harbor 解析 / override / 锚点 / store / episode 运行器。

全部 hermetic：episode 运行器注入假 VM、假 exec_fn、假 Harbor；不依赖
firecracker、docker、Linux /proc、真实 Harbor。真机验收见 README「H2 验收」。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from minisweharness.anchors import AnchorRecorder, phase_windows
from minisweharness.config import SharedMountConfig, VMConfig
from minisweharness.episode import (
    build_replay_command,
    export_events_command,
    run_episode,
)
from minisweharness.images import (
    DEFAULT_REGISTRY,
    HarborClient,
    ImageMap,
    ImageSkipped,
    MappingError,
    build_override,
    resolve_image,
)
from minisweharness.lifecycle import VMLifecycle
from minisweharness.manifest import (
    EpisodeManifest,
    source_environment_from_trajectory,
    source_image_from_trajectory,
)
from minisweharness.store import GuestStore, GuestStoreError


# ------------------------------------------------------------------ manifest


def test_manifest_validation_and_episode_id():
    manifest = EpisodeManifest(benchmark="swebench", task_id="django__django-1", attempt=0, source_episode="src")
    assert manifest.resolved_episode_id == "swebench--django__django-1--a0"
    with pytest.raises(ValueError, match="unknown manifest fields"):
        EpisodeManifest.from_dict({"benchmark": "b", "task_id": "t", "nope": 1})
    with pytest.raises(ValueError, match="requires source_episode"):
        EpisodeManifest(benchmark="b", task_id="t")
    with pytest.raises(ValueError, match="mode"):
        EpisodeManifest(benchmark="b", task_id="t", mode="bogus", source_episode="s")


def test_source_extraction_from_trajectory(tmp_path: Path):
    trajectory = {
        "info": {
            "config": {
                "environment": {
                    "runtime": "bounded Docker",
                    "image": "agentos-core-bench:local",
                    "user": 1000,
                    "cwd": "/workspace",
                }
            }
        }
    }
    assert source_image_from_trajectory(trajectory) == "agentos-core-bench:local"
    assert source_environment_from_trajectory(trajectory)["user"] == 1000
    assert source_image_from_trajectory({"info": {"config": {"environment": {}}}}) == ""


# ------------------------------------------------------------------ 映射表 + Harbor


def _image_map(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "image_map.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


class FakeHarbor:
    def __init__(self, tags_by_repo: dict[str, list[str]], digests_by_repo: dict[str, str] | None = None):
        self.tags_by_repo = tags_by_repo
        self.digests_by_repo = digests_by_repo or {}

    def list_tags(self, repo_path: str) -> list[str]:
        return self.tags_by_repo.get(repo_path, [])

    def digest(self, repo_path: str, tag: str) -> str:
        return self.digests_by_repo.get(f"{repo_path}:{tag}", "sha256:deadbeef")


def test_image_map_lookup_and_wildcard(tmp_path: Path):
    path = _image_map(
        tmp_path,
        [
            {"benchmark": "swebench", "task_id": "t-1", "docker_image": "swebench/repo-1"},
            {"benchmark": "core-bench", "task_id": "", "docker_image": "core-bench/all"},
        ],
    )
    image_map = ImageMap.from_jsonl(path)
    assert image_map.lookup("swebench", "t-1").docker_image == "swebench/repo-1"
    assert image_map.lookup("swebench", "missing") is None
    assert image_map.lookup("core-bench", "any").docker_image == "core-bench/all"


def test_image_map_rejects_multiple_wildcards(tmp_path: Path):
    path = _image_map(
        tmp_path,
        [
            {"benchmark": "b", "task_id": "", "docker_image": "b/a"},
            {"benchmark": "b", "task_id": "", "docker_image": "b/c"},
        ],
    )
    with pytest.raises(MappingError, match="wildcard"):
        ImageMap.from_jsonl(path)


def test_image_map_rejects_unknown_fields(tmp_path: Path):
    path = _image_map(tmp_path, [{"benchmark": "b", "task_id": "t", "docker_image": "b/a", "junk": 1}])
    with pytest.raises(MappingError, match="unknown"):
        ImageMap.from_jsonl(path)


def test_resolve_image_unique_tag(tmp_path: Path):
    entry = ImageMap.from_jsonl(
        _image_map(tmp_path, [{"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}])
    ).lookup("b", "t")
    harbor = FakeHarbor({"b/repo": ["v1"]}, {"b/repo:v1": "sha256:abc"})
    resolution = resolve_image(entry, registry=DEFAULT_REGISTRY, harbor=harbor, source_image="orig:local")
    assert not isinstance(resolution, ImageSkipped)
    assert resolution.image == f"{DEFAULT_REGISTRY}/b/repo:v1"
    assert resolution.digest == "sha256:abc"
    assert resolution.image_source == "harbor_direct"
    assert resolution.source_image == "orig:local"


def test_resolve_image_ambiguous_skips(tmp_path: Path):
    entry = ImageMap.from_jsonl(
        _image_map(tmp_path, [{"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}])
    ).lookup("b", "t")
    harbor = FakeHarbor({"b/repo": ["v1", "v2"]})
    result = resolve_image(entry, registry=DEFAULT_REGISTRY, harbor=harbor)
    assert isinstance(result, ImageSkipped)
    assert result.reason == "ambiguous_tags"
    assert result.tags == ("v1", "v2")


def test_resolve_image_zero_tags_errors(tmp_path: Path):
    entry = ImageMap.from_jsonl(
        _image_map(tmp_path, [{"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}])
    ).lookup("b", "t")
    with pytest.raises(MappingError, match="no tags"):
        resolve_image(entry, registry=DEFAULT_REGISTRY, harbor=FakeHarbor({}))


def test_harbor_client_project_repo_parsing():
    assert HarborClient._project_repo("swebench/repo-1") == ("swebench", "repo-1")
    with pytest.raises(MappingError, match="project/repo"):
        HarborClient._project_repo("no-slash")


def test_harbor_client_basic_and_bearer_auth():
    basic = HarborClient("harbor.test", username="robot$h2", password="secret")
    expected = base64.b64encode(b"robot$h2:secret").decode("ascii")
    assert basic._headers == {"Authorization": f"Basic {expected}"}
    bearer = HarborClient("harbor.test", token="token123")
    assert bearer._headers == {"Authorization": "Bearer token123"}
    with pytest.raises(ValueError, match="mutually exclusive"):
        HarborClient("harbor.test", token="token123", username="u", password="p")


def test_harbor_client_digest_returns_matching_artifact_digest():
    client = HarborClient("harbor.test")
    client._get = lambda path: [
        {"digest": "sha256:abc", "tags": [{"name": "other"}]},
        {"digest": "sha256:def", "tags": [{"name": "local"}]},
    ]
    assert client.digest("core-bench/agentos-core-bench", "local") == "sha256:def"
    assert client.digest("core-bench/agentos-core-bench", "missing") == ""


# ------------------------------------------------------------------ override


def test_build_override_whitelist_translation():
    override = build_override(
        f"{DEFAULT_REGISTRY}/core-bench/all:v1",
        source_environment={"runtime": "bounded Docker", "image": "agentos-core-bench:local", "user": 1000, "cwd": "/workspace"},
    )
    assert override["environment_class"] == "docker"
    assert override["image"] == f"{DEFAULT_REGISTRY}/core-bench/all:v1"
    assert override["cwd"] == "/workspace"
    assert "--user" in override["run_args"] and "1000" in override["run_args"]
    assert "--rm" in override["run_args"]
    assert "runtime" not in override  # bounded Docker 必须丢弃
    assert "timeout" not in override  # 源缺失不注入


# ------------------------------------------------------------------ 锚点


def test_anchor_recorder_and_phase_windows(tmp_path: Path):
    recorder = AnchorRecorder(tmp_path / "anchors.jsonl", "ep-1")
    for phase in ("warmup_start", "warmup_done", "episode_start", "episode_end"):
        recorder.mark(phase)
    with pytest.raises(ValueError, match="already recorded"):
        recorder.mark("episode_start")
    anchors = [json.loads(line) for line in (tmp_path / "anchors.jsonl").read_text().splitlines()]
    assert len(anchors) == 4
    assert all(a["episode"] == "ep-1" and a["layer"] == "host_harness" for a in anchors)
    windows = phase_windows(anchors)
    assert windows["warmup_start"][0] <= windows["warmup_done"][1]
    assert windows["episode_start"][0] <= windows["episode_end"][1]


# ------------------------------------------------------------------ guest store


def _put_blob(store_dir: Path, payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(raw).hexdigest()
    (store_dir / "blobs").mkdir(parents=True, exist_ok=True)
    (store_dir / "blobs" / f"{key}.gz").write_bytes(gzip.compress(raw))
    return key


def _make_store(store_dir: Path, episode: str, *, final: bool) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store_dir / "index.sqlite3")
    db.execute(
        "CREATE TABLE events (episode TEXT, kind TEXT, round INTEGER, payload TEXT,"
        " created REAL, wall_ts REAL, monotonic_ts REAL, layer TEXT)"
    )
    key = _put_blob(store_dir, {"final": final})
    db.execute(
        "INSERT INTO events(episode, kind, round, payload, created, wall_ts, monotonic_ts, layer)"
        " VALUES (?, 'session_snapshot', NULL, ?, 0, 0, 0, 'guest_cgroup')",
        (episode, key),
    )
    db.commit()
    db.close()


def test_guest_store_completed_and_exported_count(tmp_path: Path):
    store_dir = tmp_path / "store"
    _make_store(store_dir, "ep-1", final=True)
    store = GuestStore(store_dir)
    assert store.completed("ep-1") is True
    assert store.completed("other") is False
    assert store.event_count("ep-1") == 1
    events = tmp_path / "guest_events.jsonl"
    events.write_text('{"kind":"session_snapshot"}\n')
    assert store.exported_count("ep-1", events) == 1
    events.write_text('{"kind":"a"}\n{"kind":"b"}\n')
    with pytest.raises(GuestStoreError, match="row mismatch"):
        store.exported_count("ep-1", events)


def test_guest_store_incomplete_and_missing_db(tmp_path: Path):
    _make_store(tmp_path / "store", "ep-1", final=False)
    assert GuestStore(tmp_path / "store").completed("ep-1") is False
    with pytest.raises(GuestStoreError, match="not found"):
        GuestStore(tmp_path / "empty")


# ------------------------------------------------------------------ 命令组装


def test_replay_and_export_commands():
    manifest = EpisodeManifest(
        benchmark="swebench",
        task_id="django__django-1",
        source_episode="src",
        profile_digest="digest123",
    )
    command = build_replay_command(
        manifest,
        store_guest_path="/mnt/share/store",
        output_guest_path="/mnt/share/trajectory.json",
        environment_config_guest_path="/mnt/share/override.json",
    )
    joined = " ".join(command)
    assert command[:4] == ["python3", "-m", "minisweagent.run.replay", "run"]
    assert "--store /mnt/share/store" in joined
    assert "--source src" in joined
    assert "--episode swebench--django__django-1--a0" in joined
    assert "--environment-config /mnt/share/override.json" in joined
    assert "--profile-digest digest123" in joined
    export = export_events_command("/mnt/share/store", "ep-1", "/mnt/share/guest_events.jsonl")
    assert export[:4] == ["python3", "-m", "minisweagent.run.replay", "export-events"]
    assert "--output /mnt/share/guest_events.jsonl" in " ".join(export)


# ------------------------------------------------------------------ episode 运行器


class FakeProcess:
    pid = 4242


class FakeVM:
    process = FakeProcess()
    virtiofsd_process = None

    def __init__(self, config=None):
        self.lifecycle = VMLifecycle(vm_id="fake", vmm="firecracker")
        self.registry_auth_calls = []
        shared = config.shared_mount if config is not None else None
        self.host_root = Path(shared.host_dir) if shared is not None else None
        self.guest_root = shared.guest_path if shared is not None else "/mnt/share"

    def create(self):
        self.lifecycle.mark("create_start")
        self.lifecycle.mark("ready")

    def guest_exec(self, command, timeout=None):
        raise AssertionError("guest_exec should not be called when exec_fn is injected")

    def destroy(self):
        self.lifecycle.mark("destroy_confirmed")

    def docker_login(self, registry, username, password):
        self.registry_auth_calls.append(("login", registry, username, password))

    def docker_logout(self, registry):
        self.registry_auth_calls.append(("logout", registry))

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


def _vm_config(tmp_path: Path) -> VMConfig:
    return VMConfig.from_dict(
        {
            "kernel_image": "/k/vmlinux",
            "root_drive": "/k/rootfs.ext4",
            "shared_mount": SharedMountConfig(host_dir=str(tmp_path / "share")).__dict__,
        }
    )


def _source_store(tmp_path: Path, episode_id: str) -> Path:
    source = tmp_path / "source_store"
    _make_store(source, episode_id, final=True)
    return source


def test_run_episode_completed_flow(tmp_path: Path):
    image_map_path = _image_map(tmp_path, [{"benchmark": "swebench", "task_id": "t", "docker_image": "swebench/repo"}])
    episode_dir = tmp_path / "run" / "episodes" / "ep-h2"
    calls: list[str] = []

    def execute(command: str, timeout: float) -> subprocess.CompletedProcess:
        calls.append(command)
        if "export-events" in command:
            (episode_dir / "shared" / "guest_events.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (episode_dir / "shared" / "guest_events.jsonl").write_text('{"kind":"session_snapshot","final":true}\n')
        return subprocess.CompletedProcess(command, 0, "", "")

    manifest = EpisodeManifest(
        benchmark="swebench",
        task_id="t",
        source_episode="src",
        episode_id="ep-h2",
        source_store=str(_source_store(tmp_path, "ep-h2")),
    )
    record = run_episode(
        manifest,
        _vm_config(tmp_path),
        tmp_path / "run",
        image_map_path=image_map_path,
        harbor=FakeHarbor({"swebench/repo": ["v1"]}, {"swebench/repo:v1": "sha256:abc"}),
        exec_fn=execute,
        vm_factory=lambda cfg, vid, keep: FakeVM(cfg),
    )

    assert record["status"] == "completed", record
    assert record["image"] == f"{DEFAULT_REGISTRY}/swebench/repo:v1"
    assert record["image_digest"] == "sha256:abc"
    assert record["image_source"] == "harbor_direct"
    assert record["image_override"]["environment_class"] == "docker"
    assert any("docker pull " in c and f"{DEFAULT_REGISTRY}/swebench/repo:v1" in c for c in calls)
    assert not any("docker tag " in c for c in calls)  # 主方案无 tag
    assert any("minisweagent.run.replay run" in c and "--environment-config" in c for c in calls)
    for name in ("episode.json", "vm_lifecycle.json", "anchors.jsonl"):
        assert (episode_dir / name).is_file(), name
    assert (episode_dir / "shared" / "guest_events.jsonl").is_file()
    assert (episode_dir / "shared" / "override.json").is_file()

    run_manifest = json.loads((tmp_path / "run" / "run_manifest.json").read_text())
    assert run_manifest["episodes"] == [{"episode_id": "ep-h2", "status": "completed", "slot_index": None}]
    assert run_manifest["image_map"] == {"swebench/repo": {"tag": "v1", "digest": "sha256:abc"}}
    assert set(run_manifest["host"]) == {"uname", "cpu_model", "mem_bytes"}
    assert isinstance(run_manifest["started_wall"], (int, float))


def test_run_episode_missing_mapping_errors_before_vm(tmp_path: Path):
    image_map_path = _image_map(tmp_path, [{"benchmark": "other", "task_id": "t", "docker_image": "other/repo"}])
    manifest = EpisodeManifest(benchmark="swebench", task_id="t", source_episode="src")
    with pytest.raises(MappingError, match="no image_map entry"):
        run_episode(
            manifest,
            _vm_config(tmp_path),
            tmp_path / "run",
            image_map_path=image_map_path,
            harbor=FakeHarbor({}),
            vm_factory=lambda cfg, vid, keep: FakeVM(cfg),
        )


def test_run_episode_private_registry_logs_in_and_out(tmp_path: Path):
    image_map_path = _image_map(tmp_path, [{"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}])
    episode_dir = tmp_path / "run" / "episodes" / "ep-private"
    calls = []
    vms = []

    def execute(command: str, timeout: float) -> subprocess.CompletedProcess:
        calls.append(command)
        if "export-events" in command:
            (episode_dir / "shared" / "guest_events.jsonl").write_text(
                '{"kind":"session_snapshot","final":true}\n'
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def vm_factory(cfg, vid, keep):
        vm = FakeVM(cfg)
        vms.append(vm)
        return vm

    manifest = EpisodeManifest(
        benchmark="b",
        task_id="t",
        source_episode="src",
        episode_id="ep-private",
        source_store=str(_source_store(tmp_path, "ep-private")),
    )
    record = run_episode(
        manifest,
        _vm_config(tmp_path),
        tmp_path / "run",
        registry="harbor.private:12443",
        image_map_path=image_map_path,
        harbor=FakeHarbor({"b/repo": ["v1"]}),
        registry_username="robot$h2",
        registry_password="secret",
        exec_fn=execute,
        vm_factory=vm_factory,
    )

    assert record["status"] == "completed", record
    assert vms[0].registry_auth_calls == [
        ("login", "harbor.private:12443", "robot$h2", "secret"),
        ("logout", "harbor.private:12443"),
    ]
    assert all("secret" not in command for command in calls)
    assert "secret" not in json.dumps(record)


def test_run_episode_skipped_on_ambiguous_tags(tmp_path: Path):
    image_map_path = _image_map(tmp_path, [{"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}])
    manifest = EpisodeManifest(benchmark="b", task_id="t", source_episode="src", episode_id="ep-skip")
    record = run_episode(
        manifest,
        _vm_config(tmp_path),
        tmp_path / "run",
        image_map_path=image_map_path,
        harbor=FakeHarbor({"b/repo": ["v1", "v2"]}),
        vm_factory=lambda cfg, vid, keep: FakeVM(cfg),
    )
    assert record["status"] == "skipped"
    assert record["skip_reason"] == "ambiguous_tags"
    assert record["image_tags"] == ["v1", "v2"]
    assert record["image"] is None
    # skipped 不进 VM：无 lifecycle/anchors/vm_samples
    episode_dir = tmp_path / "run" / "episodes" / "ep-skip"
    assert (episode_dir / "episode.json").is_file()
    assert not (episode_dir / "vm_lifecycle.json").exists()
    assert not (episode_dir / "anchors.jsonl").exists()


def test_run_episode_failure_closes_anchor_window(tmp_path: Path):
    image_map_path = _image_map(tmp_path, [{"benchmark": "b", "task_id": "t", "docker_image": "b/repo"}])

    def execute(command: str, timeout: float) -> subprocess.CompletedProcess:
        if "minisweagent.run.replay run" in command:
            return subprocess.CompletedProcess(command, 1, "", "boom")
        return subprocess.CompletedProcess(command, 0, "", "")

    manifest = EpisodeManifest(
        benchmark="b",
        task_id="t",
        source_episode="src",
        episode_id="ep-fail",
        source_store=str(_source_store(tmp_path, "ep-fail")),
    )
    record = run_episode(
        manifest,
        _vm_config(tmp_path),
        tmp_path / "run",
        image_map_path=image_map_path,
        harbor=FakeHarbor({"b/repo": ["v1"]}),
        exec_fn=execute,
        vm_factory=lambda cfg, vid, keep: FakeVM(cfg),
    )
    assert record["status"] == "failed"
    assert "boom" in record["failure"]
    anchors = [
        json.loads(line)
        for line in (tmp_path / "run" / "episodes" / "ep-fail" / "anchors.jsonl").read_text().splitlines()
    ]
    assert any(a["phase"] == "episode_end" for a in anchors)
