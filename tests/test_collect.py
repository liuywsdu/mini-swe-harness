"""collect 测试：按 run_manifest 筛选成功 episode 并归档 episodes 目录与 manifest。

全部 hermetic：只操作 tmp 目录内伪造的 run/manifest 布局，不依赖 VM / docker。
"""

from __future__ import annotations

import json
from pathlib import Path

from minisweharness.cli import main
from minisweharness.collect import collect, default_manifest_dir


def _make_run(root: Path, episodes: dict[str, str]) -> Path:
    """构造 batch 布局 <root>/runs/batch-1 + <root>/manifests；episodes: {id: status}。"""
    run_dir = root / "runs" / "batch-1"
    manifest_dir = root / "manifests"
    run_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "batch-1",
                "episodes": [
                    {"episode_id": eid, "status": st, "slot_index": 0}
                    for eid, st in episodes.items()
                ],
            }
        )
    )
    for eid in episodes:
        ep_dir = run_dir / "episodes" / eid
        ep_dir.mkdir(parents=True)
        (ep_dir / "episode.json").write_text("{}")
        (manifest_dir / f"{eid}.json").write_text("{broken")  # 走文件名回退匹配
    return run_dir


def test_collect_moves_only_completed(tmp_path: Path):
    run_dir = _make_run(
        tmp_path,
        {
            "core-bench--capsule-0504157--a0": "completed",
            "core-bench--capsule-0851068--a0": "failed",
        },
    )
    dest = tmp_path / "runs" / "batch-1-success"
    summary = collect(run_dir / "run_manifest.json", dest)

    assert summary.matched_episodes == ["core-bench--capsule-0504157--a0"]
    assert (dest / "episodes" / "core-bench--capsule-0504157--a0" / "episode.json").is_file()
    assert (dest / "manifests" / "core-bench--capsule-0504157--a0.json").is_file()
    # 失败的 episode 留在原地
    assert (run_dir / "episodes" / "core-bench--capsule-0851068--a0").is_dir()
    assert not (dest / "episodes" / "core-bench--capsule-0851068--a0").exists()
    # 源 run_manifest 不被修改
    source = json.loads((run_dir / "run_manifest.json").read_text())
    assert len(source["episodes"]) == 2
    # 目标写过滤副本
    filtered = json.loads((dest / "run_manifest.json").read_text())
    assert [e["episode_id"] for e in filtered["episodes"]] == ["core-bench--capsule-0504157--a0"]


def test_collect_copy_keeps_sources(tmp_path: Path):
    run_dir = _make_run(tmp_path, {"swe-bench--django__django-11019--a0": "completed"})
    dest = tmp_path / "out"
    collect(run_dir / "run_manifest.json", dest, move=False)
    assert (run_dir / "episodes" / "swe-bench--django__django-11019--a0").is_dir()
    assert (dest / "episodes" / "swe-bench--django__django-11019--a0").is_dir()


def test_collect_dry_run_no_changes(tmp_path: Path):
    run_dir = _make_run(tmp_path, {"core-bench--capsule-0504157--a0": "completed"})
    dest = tmp_path / "out"
    summary = collect(run_dir / "run_manifest.json", dest, dry_run=True)
    assert summary.matched_episodes == ["core-bench--capsule-0504157--a0"]
    assert not dest.exists()
    assert (run_dir / "episodes" / "core-bench--capsule-0504157--a0").is_dir()


def test_collect_default_manifest_dir(tmp_path: Path):
    run_dir = _make_run(tmp_path, {"core-bench--capsule-0504157--a0": "completed"})
    assert default_manifest_dir(run_dir) == tmp_path / "manifests"


def test_collect_reports_missing_episode_dir(tmp_path: Path):
    run_dir = _make_run(tmp_path, {"core-bench--capsule-0504157--a0": "completed"})
    import shutil

    shutil.rmtree(run_dir / "episodes" / "core-bench--capsule-0504157--a0")
    summary = collect(run_dir / "run_manifest.json", tmp_path / "out")
    assert summary.missing_episode_dirs == ["core-bench--capsule-0504157--a0"]


def test_collect_cli_entrypoint(tmp_path: Path):
    run_dir = _make_run(tmp_path, {"core-bench--capsule-0504157--a0": "completed"})
    dest = tmp_path / "out"
    rc = main(
        [
            "collect",
            "--run-manifest",
            str(run_dir / "run_manifest.json"),
            "--dest",
            str(dest),
        ]
    )
    assert rc == 0
    assert (dest / "run_manifest.json").is_file()
