"""双时间戳（wall + monotonic）工具。

横向前提①（计划文档 §2）：所有事件带 wall+monotonic 双时间戳。
wall 用于跨进程/跨机器对齐，monotonic 用于同机窗口差分。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class DualTimestamp:
    wall: float
    """wall clock（time.time()），跨进程可比。"""

    monotonic: float
    """monotonic clock（time.monotonic()），同机差分无跳变。"""

    def to_dict(self) -> dict:
        return {"wall": self.wall, "monotonic": self.monotonic}


def now() -> DualTimestamp:
    """同一时刻取双时钟，避免两次调用的取整误差分开累积。"""
    return DualTimestamp(wall=time.time(), monotonic=time.monotonic())
