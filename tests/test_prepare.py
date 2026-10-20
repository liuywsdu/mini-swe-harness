"""prepare 测试：traces 扫描 / 映射表覆盖校验 / 幂等 import / manifest 批量生成。

全部 hermetic：FakeStoreImporter 不依赖 mini-swe-agent；不启动真实 VM。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minisweharness.cli import main
from minisweharness.images import ImageMap
from minisweharness.manifest import EpisodeManifest
from minisweharness.prepare import prepare, scan_trace_candidates, source_episode_id


class FakeStoreImporter:
    """内存 store：记录 episode；fail 集合内的 episode id import 时抛错。"""

    def __init__(self, fail: set[str] | None = None):
        self.stores: dict[Path, dict[str, dict]] = {}
        self.fail = fail or set()
        self.calls: list[tuple[Path, str]] = []

    def exists(self, store_path: Path, episode_id: str) -> bool:
        return episode_id in self.stores.get(Path(store_path), {})

    def import_episode(
        self,
        trajectory: Path,
        store_path: Path,
        episode_id: str,
        *,
        benchmark: str,
        task_id: str,
        profile_digest: str = "",
    ) -> dict:
        self.calls.append((Path(store_path), episode_id))
        if episode_id in self.fail:
            raise ValueError("not a mini tool-call trajectory")
        self.stores.setdefault(Path(store_path), {})[episode_id] = {
            "benchmark": benchmark,
            "task_id": task_id,
            "profile_digest": profile_digest,
        }
        return {"rounds": 3}


def _traces(root: Path) -> Path:
    """布局：benchA/{t1,t2}、benchB/{t1} 有 trajectory；benchB/notask 没有。"""
    for benchmark, tasks in {"benchA": ["t1", "t2"], "benchB": ["t1", "notask"]}.items():
        for task in tasks:
            d = root / "traces" / benchmark / task
            d.mkdir(parents=True)
            if task != "notask":
                (d / "trajectory.json").write_text(json.dumps({"messages": [], "info": {}}))
    return root / "traces"


def _image_map(root: Path, lines: list[dict]) -> Path:
    path = root / "image_map.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


@pytest.fixture
def layout(tmp_path: Path):
    return {
        "traces": _traces(tmp_path),
        "image_map": _image_map(
            tmp_path,
            [
                {"benchmark": "benchA", "task_id": None, "docker_image": "benchA/img"},
                {"benchmark": "benchB", "task_id": "t1", "docker_image": "benchB/img-t1"},
            ],
        ),
        "store_dir": tmp_path / "stores",
        "manifest_dir": tmp_path / "manifests",
    }


def _run(importer, layout, **kwargs):
    return prepare(
        layout["traces"],
        layout["image_map"],
        layout["store_dir"],
        layout["manifest_dir"],
        importer=importer,
        **kwargs,
    )


# ------------------------------------------------------------------ 扫描


def test_scan_collects_candidates_and_no_trajectory(layout):
    candidates, no_trajectory = scan_trace_candidates(layout["traces"])
    assert {(c["benchmark"], c["task_id"]) for c in candidates} == {
        ("benchA", "t1"),
        ("benchA", "t2"),
        ("benchB", "t1"),
    }
    assert no_trajectory == ["benchB/notask"]


def test_image_map_null_task_id_is_wildcard(tmp_path: Path):
    path = _image_map(tmp_path, [{"benchmark": "b", "task_id": None, "docker_image": "b/img"}])
    image_map = ImageMap.from_jsonl(path)
    assert image_map.lookup("b", "anything").docker_image == "b/img"


# ------------------------------------------------------------------ prepare


def test_prepare_imports_and_writes_manifests(layout):
    importer = FakeStoreImporter()
    report = _run(importer, layout, profile_digest="digest-1")
    assert report["candidates"] == 3
    assert report["no_trajectory"] == ["benchB/notask"]
    assert sorted(report["imported"]) == ["benchA--t1", "benchA--t2", "benchB--t1"]
    assert report["existing"] == []
    assert report["missing_image"] == []
    assert len(report["manifests"]) == 3

    # store 每源 episode 一份（注入只上传该 episode 需要的数据），identity metadata 写入
    assert set(importer.stores) == {
        layout["store_dir"] / "benchA" / "benchA--t1",
        layout["store_dir"] / "benchA" / "benchA--t2",
        layout["store_dir"] / "benchB" / "benchB--t1",
    }
    assert importer.stores[layout["store_dir"] / "benchA" / "benchA--t1"]["benchA--t1"]["profile_digest"] == "digest-1"

    # manifest：episode_id 留空（harness 自动生成，与 source_episode 区分）
    manifest_path = layout["manifest_dir"] / "benchA--t1--a0.json"
    manifest = EpisodeManifest.from_json(manifest_path)
    assert manifest.source_episode == "benchA--t1"
    assert manifest.episode_id == ""
    assert manifest.resolved_episode_id == "benchA--t1--a0"  # 与 source 必然不同
    assert manifest.mode == "replay"
    assert manifest.source_store == str((layout["store_dir"] / "benchA" / "benchA--t1").resolve())
    assert manifest.trajectory.endswith("benchA/t1/trajectory.json")
    assert manifest.profile_digest == "digest-1"


def test_prepare_is_idempotent(layout):
    importer = FakeStoreImporter()
    _run(importer, layout)
    second = _run(importer, layout)
    assert second["imported"] == []
    assert sorted(second["existing"]) == ["benchA--t1", "benchA--t2", "benchB--t1"]
    assert len(second["manifests"]) == 3  # manifest 覆写重写，幂等


def test_prepare_store_layout_is_per_episode(layout):
    """每个 store 只含自己的源 episode：episode 注入上传量与 task 总数解耦。"""
    importer = FakeStoreImporter()
    report = _run(importer, layout)
    assert report["store_layout"] == "per_episode"
    for store_path, episodes in importer.stores.items():
        assert len(episodes) == 1, f"{store_path} 应只含一条源 episode"
        assert store_path.name == next(iter(episodes)), "store 目录名即源 episode id"
    # 不同 benchmark 不共享 store
    assert layout["store_dir"] / "benchA" / "benchB--t1" not in importer.stores


def test_prepare_missing_image_map_entry_excluded(layout):
    layout["image_map"] = _image_map(
        layout["image_map"].parent, [{"benchmark": "benchA", "task_id": None, "docker_image": "benchA/img"}]
    )
    report = _run(FakeStoreImporter(), layout)
    assert report["missing_image"] == [{"benchmark": "benchB", "task_id": "t1"}]
    assert "benchB--t1" not in report["imported"]
    assert not (layout["manifest_dir"] / "benchB--t1--a0.json").is_file()
    assert len(report["manifests"]) == 2


def test_prepare_import_failure_reported_and_others_continue(layout):
    importer = FakeStoreImporter(fail={"benchA--t2"})
    report = _run(importer, layout)
    assert [f["episode"] for f in report["import_failed"]] == ["benchA--t2"]
    assert "ValueError" in report["import_failed"][0]["error"]
    assert sorted(report["imported"]) == ["benchA--t1", "benchB--t1"]
    assert not (layout["manifest_dir"] / "benchA--t2--a0.json").is_file()


def test_prepare_attempts_writes_one_manifest_per_attempt(layout):
    report = _run(FakeStoreImporter(), layout, attempts=2)
    assert len(report["manifests"]) == 6
    assert (layout["manifest_dir"] / "benchA--t1--a1.json").is_file()
    assert EpisodeManifest.from_json(layout["manifest_dir"] / "benchA--t1--a1.json").attempt == 1


def test_prepare_dry_run_writes_nothing(layout):
    report = _run(FakeStoreImporter(), layout, dry_run=True)
    assert report["dry_run"] is True
    assert len(report["manifests"]) == 3  # 报告仍列出将生成的路径
    assert not layout["store_dir"].exists()
    assert not layout["manifest_dir"].exists()


def test_prepare_skip_import_requires_existing_store_episode(layout):
    # skip_import 且 store 已有源 episode → 生成 manifest
    importer = FakeStoreImporter()
    _run(importer, layout)
    report = _run(importer, layout, skip_import=True)
    assert len(report["manifests"]) == 3
    # skip_import 且 store 缺源 episode → 记 store_missing 且不进 manifest
    fresh = _run(FakeStoreImporter(), layout, skip_import=True)
    assert sorted(fresh["store_missing"]) == ["benchA--t1", "benchA--t2", "benchB--t1"]
    assert fresh["manifests"] == []


def test_source_episode_id_format():
    assert source_episode_id("swe-bench", "django__django-11099") == "swe-bench--django__django-11099"
    assert source_episode_id("swe-bench", "t", run=2) == "swe-bench--t--r2"


def test_nested_runs_layout_multi_run_naming(tmp_path: Path):
    """嵌套 run 布局（runs/<run_id>/.../trajectory.json）：每份 trajectory 一个候选。"""
    for run_id in ("20261004-a", "20261004-b"):
        d = tmp_path / "traces" / "benchA" / "t1" / "runs" / run_id / "mini-swe-agent" / "t1__hash"
        d.mkdir(parents=True)
        (d / "trajectory.json").write_text("{}")
    (tmp_path / "traces" / "benchA" / "t1" / "task.json").write_text("{}")
    candidates, no_trajectory = scan_trace_candidates(tmp_path / "traces")
    assert no_trajectory == []
    assert [(c["task_id"], c["run"]) for c in candidates] == [("t1", 0), ("t1", 1)]

    image_map = _image_map(tmp_path, [{"benchmark": "benchA", "task_id": None, "docker_image": "benchA/img"}])
    report = prepare(
        tmp_path / "traces", image_map, tmp_path / "stores", tmp_path / "manifests",
        importer=FakeStoreImporter(),
    )
    # 两个 run：首 run episode 无后缀，第二个 --r1；manifest 文件名不冲突
    assert sorted(report["imported"]) == ["benchA--t1", "benchA--t1--r1"]
    first = EpisodeManifest.from_json(tmp_path / "manifests" / "benchA--t1--a0.json")
    assert first.source_episode == "benchA--t1"
    assert first.episode_id == ""
    second = EpisodeManifest.from_json(tmp_path / "manifests" / "benchA--t1--r1--a0.json")
    assert second.source_episode == "benchA--t1--r1"
    assert second.source_store == str((tmp_path / "stores" / "benchA" / "benchA--t1--r1").resolve())
    assert second.episode_id == "benchA--t1--r1--a0"  # 带 attempt，episode 目录唯一
    # 不同 run 指向不同 trajectory 文件
    assert first.trajectory != second.trajectory


# ------------------------------------------------------------------ CLI


def test_prepare_cli_end_to_end(layout, tmp_path: Path, monkeypatch, capsys):
    fake = FakeStoreImporter()
    monkeypatch.setattr("minisweharness.prepare.MiniStoreImporter", lambda: fake)
    rc = main(
        [
            "prepare",
            "--traces-dir",
            str(layout["traces"]),
            "--image-map",
            str(layout["image_map"]),
            "--store-dir",
            str(layout["store_dir"]),
            "--manifest-dir",
            str(layout["manifest_dir"]),
        ]
    )
    assert rc == 0
    assert (layout["manifest_dir"] / "benchA--t1--a0.json").is_file()
    report = json.loads(capsys.readouterr().out)
    assert report["candidates"] == 3


def test_prepare_cli_exit_nonzero_on_import_failure(layout, monkeypatch, capsys):
    fake = FakeStoreImporter(fail={"benchA--t1"})
    monkeypatch.setattr("minisweharness.prepare.MiniStoreImporter", lambda: fake)
    rc = main(
        [
            "prepare",
            "--traces-dir",
            str(layout["traces"]),
            "--image-map",
            str(layout["image_map"]),
            "--store-dir",
            str(layout["store_dir"]),
            "--manifest-dir",
            str(layout["manifest_dir"]),
        ]
    )
    assert rc == 1
    assert "failed to import" in capsys.readouterr().err


# ------------------------------------------------------------------ replay timing 定参


def _sqlite_store_with_calls(root: Path, episode_id: str, durations: list) -> Path:
    """真实最小 index.sqlite3：calls 表按 durations 造行（None=缺 duration）。"""
    root.mkdir(parents=True, exist_ok=True)
    import sqlite3

    db = sqlite3.connect(root / "index.sqlite3")
    db.execute(
        "CREATE TABLE calls(episode TEXT, round INT, attempt INT, request TEXT, "
        "response TEXT, error TEXT, duration REAL, status TEXT, started REAL)"
    )
    for i, duration in enumerate(durations, start=1):
        db.execute(
            "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?)",
            (episode_id, i, 0, "{}", None, None, duration, "ok" if duration is not None else "pending", 0.0),
        )
    db.commit()
    db.close()
    return root


def test_source_has_missing_durations(tmp_path: Path):
    from minisweharness.prepare import source_has_missing_durations

    complete = _sqlite_store_with_calls(tmp_path / "complete", "ep", [1.0, 2.0])
    assert source_has_missing_durations(complete, "ep") is False
    incomplete = _sqlite_store_with_calls(tmp_path / "incomplete", "ep", [1.0, None])
    assert source_has_missing_durations(incomplete, "ep") is True
    assert source_has_missing_durations(tmp_path / "no-db", "ep") is True  # 无从判断按缺失处理


def test_prepare_timing_auto_writes_instant_only_when_durations_missing(layout):
    """timing=auto：外部 trace（duration 全 NULL）→ manifest 带 --timing instant。"""
    from minisweharness.prepare import timing_extra_args

    incomplete = _sqlite_store_with_calls(layout["store_dir"] / "benchA" / "benchA--t1", "benchA--t1", [None, None])
    assert timing_extra_args("auto", incomplete, "benchA--t1") == ["--timing", "instant"]

    complete = _sqlite_store_with_calls(layout["store_dir"] / "benchA" / "benchA--t2", "benchA--t2", [1.5, 2.5])
    assert timing_extra_args("auto", complete, "benchA--t2") == []


def test_prepare_timing_explicit_overrides_auto(layout):
    from minisweharness.prepare import timing_extra_args

    store = _sqlite_store_with_calls(layout["store_dir"] / "benchA" / "benchA--t1", "benchA--t1", [None])
    assert timing_extra_args("recorded", store, "benchA--t1") == []
    assert timing_extra_args("simulated", store, "benchA--t1") == ["--timing", "simulated"]
    with pytest.raises(ValueError, match="timing"):
        timing_extra_args("bogus", store, "benchA--t1")


def test_prepare_manifest_carries_timing_args(layout):
    """端到端：auto + 缺 duration 的 store → 生成的 manifest.extra_replay_args 带 --timing instant。"""
    # importer 落真实 sqlite：t1 缺 duration、t2 齐全，auto 应分别给出 instant / 无参数
    class SqliteWritingImporter(FakeStoreImporter):
        DURATIONS = {"benchA--t1": [None, None], "benchA--t2": [1.0], "benchB--t1": [None]}

        def import_episode(self, trajectory, store_path, episode_id, **kwargs):
            result = super().import_episode(trajectory, store_path, episode_id, **kwargs)
            _sqlite_store_with_calls(Path(store_path), episode_id, self.DURATIONS[episode_id])
            return result

    prepare(
        layout["traces"],
        layout["image_map"],
        layout["store_dir"],
        layout["manifest_dir"],
        importer=SqliteWritingImporter(),
        timing="auto",
    )
    manifest = EpisodeManifest.from_json(layout["manifest_dir"] / "benchA--t1--a0.json")
    assert manifest.extra_replay_args == ["--timing", "instant"]
    manifest_t2 = EpisodeManifest.from_json(layout["manifest_dir"] / "benchA--t2--a0.json")
    assert manifest_t2.extra_replay_args == []  # duration 齐全 → mini 默认 recorded
