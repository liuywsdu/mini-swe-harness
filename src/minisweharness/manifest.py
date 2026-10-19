"""Episode manifest：一次 episode 运行的输入描述（计划文档 §2 episode 矩阵）。

episode = ``(benchmark, task_id, attempt)`` 的一次运行。manifest 由 harness
消费，产出 §6 规范的 ``episode.json``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_MODES = ("record", "replay")


@dataclass(frozen=True)
class EpisodeManifest:
    benchmark: str
    task_id: str
    attempt: int = 0
    mode: str = "replay"
    """record（真实模型）或 replay（固定 tape，开销可控）。"""

    profile_digest: str = ""
    source_episode: str = ""
    """replay 源 episode id（store 内）。"""

    source_store: str = ""
    """宿主机侧源 store 目录（replay 需要；注入时复制进共享挂载）。"""

    episode_id: str = ""
    """缺省自动生成：``{benchmark}--{task_id}--a{attempt}``。"""

    trajectory: str = ""
    """宿主机侧 trajectory JSON（用于提取 source_image 与 replay 环境信息）。"""

    mini_executable: str = "python3 -m minisweagent.run.replay"
    episode_timeout_seconds: float = 3600.0

    extra_replay_args: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}")
        if self.mode == "replay" and not self.source_episode:
            raise ValueError("replay mode requires source_episode")

    @property
    def resolved_episode_id(self) -> str:
        if self.episode_id:
            return self.episode_id
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{self.benchmark}--{self.task_id}")
        return f"{safe}--a{self.attempt}"

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeManifest":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown manifest fields: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def from_json(cls, path: str | Path) -> "EpisodeManifest":
        return cls.from_dict(json.loads(Path(path).read_text()))


def source_image_from_trajectory(trajectory: dict) -> str:
    """从 trajectory JSON 的 ``info.config.environment`` 提取 trace 原名（source_image）。

    覆盖两种形态：直连 environment config（``image``）与 ExecutorEnvironment
    形态（executor / sandbox 嵌套）。local 形态无镜像 → 返回 ""。"""
    environment = trajectory.get("info", {}).get("config", {}).get("environment", {})
    candidates = [
        environment.get("image", ""),
        environment.get("executor", {}).get("image", ""),
        environment.get("sandbox", {}).get("image", ""),
    ]
    return next((image for image in candidates if image), "")


def source_environment_from_trajectory(trajectory: dict) -> dict:
    """从 trajectory JSON 提取源 environment dict（供 override 白名单翻译）。"""
    return trajectory.get("info", {}).get("config", {}).get("environment", {})
