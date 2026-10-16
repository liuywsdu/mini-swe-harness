"""阶段锚点（计划文档 §5.2 锚点契约）：切分 episode 内测量区间。

``{episode, round, action_index|null, phase, wall_ts, monotonic_ts, layer}``；
host 侧记录（wall/monotonic 均为宿主机时钟），用于：
- warm-up pull 流量与 episode 测量区间切窗（§5.4 计量口径）；
- 与 vm_samples 对齐做分层对账（H3/H4）。
"""

from __future__ import annotations

import json
from pathlib import Path

from minisweharness.timestamps import now

ANCHOR_LAYER = "host_harness"


class AnchorRecorder:
    def __init__(self, path: str | Path, episode_id: str):
        self.path = Path(path)
        self.episode_id = episode_id
        self._phases: list[str] = []

    def mark(self, phase: str, *, action_index: int | None = None, round: int | None = None) -> dict:
        if phase in self._phases:
            raise ValueError(f"anchor phase already recorded: {phase}")
        self._phases.append(phase)
        timestamp = now()
        anchor = {
            "episode": self.episode_id,
            "round": round,
            "action_index": action_index,
            "phase": phase,
            "wall_ts": timestamp.wall,
            "monotonic_ts": timestamp.monotonic,
            "layer": ANCHOR_LAYER,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as stream:
            stream.write(json.dumps(anchor, ensure_ascii=False) + "\n")
        return anchor


def phase_windows(anchors: list[dict]) -> dict[str, tuple[float, float]]:
    """相邻同名锚点对组成阶段窗口；单点阶段（如异常中断）窗口闭合为同一点。"""
    windows: dict[str, list[float]] = {}
    for anchor in anchors:
        windows.setdefault(anchor["phase"], []).append(anchor["monotonic_ts"])
    return {
        phase: (stamps[0], stamps[-1]) if len(stamps) > 1 else (stamps[0], stamps[0])
        for phase, stamps in windows.items()
        if stamps
    }
