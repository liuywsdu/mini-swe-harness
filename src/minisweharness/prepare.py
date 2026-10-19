"""prepare：traces 目录批量准备（source store import + episode manifest 生成 + 映射表覆盖校验）。

对应计划文档 §5.4「覆盖校验：每个 episode 都能从映射表解析出 image 或被显式标记
跳过，映射表完全缺失条目时在起跑前报错」。输入目录约定：

    <traces-dir>/<benchmark>/<task_id>/trajectory.json

**store 布局（每源 episode 一份）**：``<store-dir>/<benchmark>/<source_episode_id>/``
——每个 store 只含一条源 episode。episode 注入（``episode.py``）会把
``manifest.source_store`` 整个上传进 guest（VM 每 episode 新建即销毁，无法复用），
per-episode store 保证每条 episode 只搬运自己需要的数据（KB~MB 级），避免全量
benchmark store（GB 级、海量小文件）反复 scp 超时。

处理流程（幂等，可反复执行）：

1. 扫描候选 episode（缺 ``trajectory.json`` 的目录记 ``no_trajectory``）；
2. 映射表覆盖校验（复用 ``images.ImageMap``，精确匹配 → benchmark 通配回退）；
   缺失条目记 ``missing_image`` 并不进 manifest（harness 起跑前报错口径一致）；
3. 每个源 episode 一个 source store（``<store-dir>/<benchmark>/<source_episode_id>``；
   episode id 为 ``{benchmark}--{task_id}``，多 run 加 ``--rN`` 后缀；跨 attempt
   共享一份源）。已存在的 episode 跳过（``existing``）；import 失败的记
   ``import_failed`` 并进报告，不中断批量；
4. 每个 (episode, attempt) 生成一份 harness episode manifest
   （``<manifest-dir>/{benchmark}--{task_id}--a{attempt}.json``）：
   ``source_episode`` 指向 store 内源 episode，``source_store`` 指向该
   per-episode store 目录，``episode_id`` 留空由 harness 自动生成
   ``{benchmark}--{task_id}--a{attempt}``——与 source_episode 必然不同，
   避免 replay 在同一 store 内自引用（create_episode 主键冲突）。

import 本身委托 mini-swe-agent 的 ``replay import``（懒加载；宿主未安装 mini
时报清晰错误），测试注入 fake importer 即可 hermetic。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from minisweharness.images import ImageMap
from minisweharness.manifest import EpisodeManifest

DEFAULT_MINI_EXECUTABLE = "python3 -m minisweagent.run.replay"
DEFAULT_EPISODE_TIMEOUT_SECONDS = 3600.0


# ------------------------------------------------------------------ store import


class StoreImporter(Protocol):
    """source store 读写抽象：默认走 mini-swe-agent，测试注入 fake。"""

    def exists(self, store_path: Path, episode_id: str) -> bool:
        """store 内是否已有该 episode（幂等跳过依据）。"""
        ...

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
        """import 一条 trajectory 并写入结构化身份 metadata；返回 {"rounds": int}。"""
        ...


class MiniStoreImporter:
    """经 mini-sweagent ``replay import`` 落 store（懒加载）。"""

    def __init__(self) -> None:
        self._replay_module = None
        self._store_class = None

    def _load(self) -> None:
        if self._replay_module is not None:
            return
        # mini 包初始化会向 stdout 打版本横幅；prepare 的 stdout 是机器可读报告，须屏蔽
        import contextlib
        import io as _io

        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                from minisweagent.run.replay import import_trajectory
                from minisweagent.utils.replay_store import ReplayStore
        except ImportError as exc:  # pragma: no cover — 取决于宿主环境
            raise RuntimeError(
                "prepare import 需要 mini-swe-agent（from minisweagent.run.replay "
                "import import_trajectory 失败）；请先在宿主机安装 mini-swe-agent"
            ) from exc
        self._replay_module = import_trajectory
        self._store_class = ReplayStore

    def exists(self, store_path: Path, episode_id: str) -> bool:
        self._load()
        store = self._store_class(store_path)
        try:
            store.metadata(episode_id)
        except KeyError:
            return False
        return True

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
        self._load()
        store = self._store_class(store_path)
        result = self._replay_module(store, trajectory, episode_id, None, task_id)
        metadata = store.metadata(episode_id)
        metadata.update(benchmark=benchmark, attempt=0, profile_digest=profile_digest)
        store.update_metadata(episode_id, metadata)
        return {"rounds": result["rounds"]}


# ------------------------------------------------------------------ 扫描与准备


def scan_trace_candidates(traces_dir: str | Path) -> tuple[list[dict], list[str]]:
    """扫描 ``<benchmark>/<task_id>/`` 下的全部 ``trajectory.json``，返回 (候选, 无 trajectory 的 task)。

    兼容两种布局：扁平（``<task>/trajectory.json``）与嵌套 run 布局
    （``<task>/runs/<run_id>/.../trajectory.json``）。同一 task 多个 run 时
    每份 trajectory 是一个候选：排序首个 run 无后缀（episode
    ``{benchmark}--{task_id}``），其余追加 ``--r{seq}``（seq 从 1 起），
    保证 source episode id 唯一且确定。
    """
    traces_dir = Path(traces_dir)
    candidates: list[dict] = []
    no_trajectory: list[str] = []
    for benchmark in sorted(traces_dir.iterdir()):
        if not benchmark.is_dir():
            continue
        for task_dir in sorted(benchmark.iterdir()):
            if not task_dir.is_dir():
                continue
            trajectories = sorted(task_dir.rglob("trajectory.json"))
            key = f"{benchmark.name}/{task_dir.name}"
            if not trajectories:
                no_trajectory.append(key)
                continue
            for seq, trajectory in enumerate(trajectories):
                candidates.append(
                    {
                        "benchmark": benchmark.name,
                        "task_id": task_dir.name,
                        "trajectory": trajectory,
                        "run": seq,
                    }
                )
    return candidates, no_trajectory


def source_episode_id(benchmark: str, task_id: str, run: int = 0) -> str:
    """store 内源 episode id：首 run 无后缀，其余 ``--r{run}``（与扫描排序一致）。"""
    base = f"{benchmark}--{task_id}"
    return base if run == 0 else f"{base}--r{run}"


def build_manifest(
    candidate: dict,
    *,
    store_path: Path,
    attempt: int,
    profile_digest: str = "",
    episode_timeout_seconds: float = DEFAULT_EPISODE_TIMEOUT_SECONDS,
    mini_executable: str = DEFAULT_MINI_EXECUTABLE,
) -> EpisodeManifest:
    """episode manifest：episode_id 留空 → harness 自动 {benchmark}--{task_id}--a{attempt}。

    多 run 候选（run>0）显式给 episode_id 加 ``--r{run}`` 标记，与 source_episode
    后缀一致，避免不同 run 的 episode 目录互相覆盖。
    """
    run = candidate.get("run", 0)
    return EpisodeManifest(
        benchmark=candidate["benchmark"],
        task_id=candidate["task_id"],
        attempt=attempt,
        mode="replay",
        profile_digest=profile_digest,
        source_episode=source_episode_id(candidate["benchmark"], candidate["task_id"], run),
        source_store=str(store_path.resolve()),
        trajectory=str(candidate["trajectory"].resolve()),
        mini_executable=mini_executable,
        episode_timeout_seconds=episode_timeout_seconds,
        # run>0 显式 episode_id 带 attempt，保证多 attempt 时 episode 目录唯一
        episode_id="" if run == 0 else f"{candidate['benchmark']}--{candidate['task_id']}--r{run}--a{attempt}",
    )


def prepare(
    traces_dir: str | Path,
    image_map_path: str | Path,
    store_dir: str | Path,
    manifest_dir: str | Path,
    *,
    attempts: int = 1,
    profile_digest: str = "",
    skip_import: bool = False,
    dry_run: bool = False,
    importer: StoreImporter | None = None,
    episode_timeout_seconds: float = DEFAULT_EPISODE_TIMEOUT_SECONDS,
    mini_executable: str = DEFAULT_MINI_EXECUTABLE,
) -> dict:
    """批量准备；返回报告 dict（也作为 CLI stdout）。"""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1: {attempts}")
    importer = importer or MiniStoreImporter()
    store_dir = Path(store_dir)
    manifest_dir = Path(manifest_dir)

    image_map = ImageMap.from_jsonl(image_map_path)
    candidates, no_trajectory = scan_trace_candidates(traces_dir)

    report: dict = {
        "traces_dir": str(Path(traces_dir).resolve()),
        "image_map": str(Path(image_map_path).resolve()),
        "candidates": len(candidates),
        "no_trajectory": no_trajectory,
        "attempts": attempts,
        "dry_run": dry_run,
        "stores": {},
        "missing_image": [],
        "imported": [],
        "existing": [],
        "import_failed": [],
        "store_missing": [],
        "manifests": [],
    }

    report["store_layout"] = "per_episode"

    # 按 benchmark 分组处理；store 每源 episode 一份（注入只上传本 episode 需要的数据）
    by_benchmark: dict[str, list[dict]] = {}
    for candidate in candidates:
        by_benchmark.setdefault(candidate["benchmark"], []).append(candidate)

    for benchmark, group in sorted(by_benchmark.items()):
        report["stores"][benchmark] = str((store_dir / benchmark).resolve())
        for candidate in group:
            episode_id = source_episode_id(
                candidate["benchmark"], candidate["task_id"], candidate.get("run", 0)
            )
            store_path = store_dir / benchmark / episode_id

            # 覆盖校验：缺失条目不进 manifest（起跑前报错口径，见 images.MappingError）
            if image_map.lookup(candidate["benchmark"], candidate["task_id"]) is None:
                report["missing_image"].append(
                    {"benchmark": candidate["benchmark"], "task_id": candidate["task_id"]}
                )
                continue

            # import（幂等）
            if not dry_run:
                try:
                    if not skip_import and not importer.exists(store_path, episode_id):
                        importer.import_episode(
                            candidate["trajectory"],
                            store_path,
                            episode_id,
                            benchmark=candidate["benchmark"],
                            task_id=candidate["task_id"],
                            profile_digest=profile_digest,
                        )
                        report["imported"].append(episode_id)
                    elif importer.exists(store_path, episode_id):
                        report["existing"].append(episode_id)
                    else:  # skip_import 且 store 缺源 episode
                        report["store_missing"].append(episode_id)
                        continue
                except Exception as exc:  # noqa: BLE001 — 单条失败不拖垮批量
                    report["import_failed"].append({"episode": episode_id, "error": f"{type(exc).__name__}: {exc}"})
                    continue

            # manifest 每 attempt 一份
            for attempt in range(attempts):
                manifest = build_manifest(
                    candidate,
                    store_path=store_path,
                    attempt=attempt,
                    profile_digest=profile_digest,
                    episode_timeout_seconds=episode_timeout_seconds,
                    mini_executable=mini_executable,
                )
                path = manifest_dir / f"{manifest.resolved_episode_id}.json"
                if not dry_run:
                    manifest_dir.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(asdict(manifest), indent=2, ensure_ascii=False) + "\n")
                report["manifests"].append(str(path.resolve()))
    return report
