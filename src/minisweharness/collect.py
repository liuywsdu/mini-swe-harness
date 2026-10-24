"""collect：按 run_manifest.json 筛选成功 episode，归档（移动/复制）其结果与 manifest。

典型用法：一次批量 run（``batch-test/runs/batch-1``）结束后，大部分 episode 因
确定性配置/数据问题失败（见 run-batch-1-report），只把 ``status == "completed"``
的 episode 结果目录（``episodes/<episode_id>/``）与对应 episode manifest 归档到
新目录（如 ``batch-test/runs/batch-1-success``），便于后续复跑/交付只含有效 trace。

归档布局（与 §6 run 目录同构）::

    <dest>/run_manifest.json          # 仅含被归档 episode 的过滤副本
    <dest>/episodes/<episode_id>/    # 自 <run-dir>/episodes/ 移动（或复制）
    <dest>/manifests/<name>.json     # 自 <manifest-dir>/ 移动（或复制）

manifest 匹配规则：扫描 ``<manifest-dir>/*.json``，解析出 resolved episode_id
（优先 manifest 内容，解析失败回退文件名 stem）与 run_manifest 中的 completed
episode_id 精确匹配。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from minisweharness.manifest import EpisodeManifest

SUCCESS_STATUS = "completed"


@dataclass
class CollectSummary:
    """归档结果统计（同时可 JSON 序列化）。"""

    run_manifest: str
    run_dir: str
    manifest_dir: str
    dest: str
    status_filter: list[str] = field(default_factory=lambda: [SUCCESS_STATUS])
    move: bool = True
    dry_run: bool = False
    matched_episodes: list[str] = field(default_factory=list)
    moved_episode_dirs: list[str] = field(default_factory=list)
    moved_manifests: list[str] = field(default_factory=list)
    missing_episode_dirs: list[str] = field(default_factory=list)
    unmatched_episodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_manifest": self.run_manifest,
            "run_dir": self.run_dir,
            "manifest_dir": self.manifest_dir,
            "dest": self.dest,
            "status_filter": self.status_filter,
            "move": self.move,
            "dry_run": self.dry_run,
            "counts": {
                "matched": len(self.matched_episodes),
                "episode_dirs_moved": len(self.moved_episode_dirs),
                "manifests_moved": len(self.moved_manifests),
                "episode_dirs_missing": len(self.missing_episode_dirs),
                "matched_without_manifest": len(self.unmatched_episodes),
            },
            "matched_episodes": self.matched_episodes,
            "moved_episode_dirs": self.moved_episode_dirs,
            "moved_manifests": self.moved_manifests,
            "missing_episode_dirs": self.missing_episode_dirs,
            "matched_without_manifest": self.unmatched_episodes,
        }


def default_manifest_dir(run_dir: Path) -> Path:
    """按 batch 布局 ``<root>/runs/<run_id>`` + ``<root>/manifests`` 猜测 manifest 目录。"""
    return run_dir.parent.parent / "manifests"


def _resolved_episode_id(manifest_path: Path) -> str:
    """manifest → episode_id：优先解析内容，失败回退文件名 stem。"""
    try:
        return EpisodeManifest.from_json(manifest_path).resolved_episode_id
    except Exception:
        return manifest_path.stem


def _transfer(src: Path, dst: Path, *, move: bool, dry_run: bool) -> None:
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(src), str(dst))
    elif src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def collect(
    run_manifest_path: str | Path,
    dest_dir: str | Path,
    *,
    manifest_dir: str | Path | None = None,
    status: list[str] | None = None,
    move: bool = True,
    dry_run: bool = False,
) -> CollectSummary:
    """筛选 run_manifest 中成功的 episode，归档其 episodes 结果目录与 manifest。

    :param run_manifest_path: ``<run-dir>/run_manifest.json`` 路径。
    :param dest_dir: 归档目标目录（自动创建）。
    :param manifest_dir: episode manifest 目录；缺省按 ``<run_dir>/../../manifests`` 猜测。
    :param status: 视为成功的状态列表（默认仅 ``completed``）。
    :param move: True 移动（默认，释放磁盘）；False 复制。
    :param dry_run: 只打印计划，不改动文件。
    """
    run_manifest_path = Path(run_manifest_path)
    dest = Path(dest_dir)
    if not run_manifest_path.is_file():
        raise FileNotFoundError(f"run_manifest not found: {run_manifest_path}")
    run_dir = run_manifest_path.parent
    manifest_dir = Path(manifest_dir) if manifest_dir else default_manifest_dir(run_dir)
    status_filter = status or [SUCCESS_STATUS]

    manifest = json.loads(run_manifest_path.read_text())
    episodes = manifest.get("episodes", [])
    matched = [e["episode_id"] for e in episodes if e.get("status") in status_filter]

    summary = CollectSummary(
        run_manifest=str(run_manifest_path),
        run_dir=str(run_dir),
        manifest_dir=str(manifest_dir),
        dest=str(dest),
        status_filter=list(status_filter),
        move=move,
        dry_run=dry_run,
        matched_episodes=sorted(matched),
    )

    # episode_id → manifest 路径（内容解析优先，文件名回退）
    manifest_by_episode: dict[str, Path] = {}
    if manifest_dir.is_dir():
        for path in sorted(manifest_dir.glob("*.json")):
            manifest_by_episode.setdefault(_resolved_episode_id(path), path)

    for episode_id in sorted(matched):
        episode_src = run_dir / "episodes" / episode_id
        episode_dst = dest / "episodes" / episode_id
        if not episode_src.is_dir():
            summary.missing_episode_dirs.append(episode_id)
        else:
            _transfer(episode_src, episode_dst, move=move, dry_run=dry_run)
            summary.moved_episode_dirs.append(episode_id)
        manifest_src = manifest_by_episode.get(episode_id)
        if manifest_src is None:
            summary.unmatched_episodes.append(episode_id)
        else:
            _transfer(manifest_src, dest / "manifests" / manifest_src.name,
                      move=move, dry_run=dry_run)
            summary.moved_manifests.append(manifest_src.name)

    # 目标目录写一份仅含被归档 episode 的 run_manifest 副本
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        filtered = {
            **manifest,
            "episodes": [
                e for e in episodes if e.get("episode_id") in set(matched)
            ],
        }
        (dest / "run_manifest.json").write_text(
            json.dumps(filtered, indent=2, ensure_ascii=False) + "\n"
        )
    return summary
