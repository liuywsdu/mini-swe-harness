"""时钟探针：读 guest 心跳文件并打 host 时间戳，产出 clock_offset（§5.1 / §6）。

mini 的 ``Heartbeat``（``utils/heartbeat.py``）周期性把 guest wall+monotonic
写到 store 旁的 ``heartbeat.json``（原子替换）。harness 周期读该文件并打
host 双时间戳：``offset_seconds = guest_wall − host_wall``；跨层对齐一律
「guest wall − offset → host wall」。

``drift_ppm`` 是 offset 随 host monotonic 变化的斜率（guest 时钟相对 host 的
漂移率，ppm），需 ≥2 个探测样本。
"""

from __future__ import annotations

import json
import statistics
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from minisweharness.timestamps import DualTimestamp, now


@dataclass
class ClockProbe:
    heartbeat_path: str | Path
    interval_seconds: float = 0.5
    clock: Callable[[], DualTimestamp] = now
    _samples: list[dict] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = None

    def collect_once(self) -> dict | None:
        path = Path(self.heartbeat_path)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        guest_wall = float(payload.get("wall_ts", 0.0))
        guest_monotonic = float(payload.get("monotonic_ts", 0.0))
        # 去重：heartbeat 未更新则跳过（guest_monotonic 精确递增）
        if self._samples and self._samples[-1]["guest_monotonic"] == guest_monotonic:
            return None
        stamp = self.clock()
        sample = {
            "guest_wall": guest_wall,
            "guest_monotonic": guest_monotonic,
            "host_wall": stamp.wall,
            "host_monotonic": stamp.monotonic,
        }
        self._samples.append(sample)
        return sample

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.collect_once()

    def start(self) -> "ClockProbe":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> "ClockProbe":
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)
        self.collect_once()  # guest 退出时留下的最终 beat
        return self

    def calibrate(self) -> dict:
        return compute_offset(self._samples)

    def __enter__(self) -> "ClockProbe":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


def compute_offset(samples: list[dict]) -> dict:
    """``{offset_seconds, drift_ppm, probe_count}``（§6 clock_sync.json）。

    offset 取中位数（抗个别读延迟抖动）；drift 为 offset 对 host monotonic 的
    最小二乘斜率 ×1e6，样本 <2 时记 null。
    """
    if not samples:
        return {"offset_seconds": None, "drift_ppm": None, "probe_count": 0}
    offsets = [sample["guest_wall"] - sample["host_wall"] for sample in samples]
    offset_seconds = statistics.median(offsets)
    drift_ppm = None
    if len(samples) >= 2:
        origin = samples[0]["host_monotonic"]
        xs = [sample["host_monotonic"] - origin for sample in samples]
        slope = _slope(xs, offsets)
        drift_ppm = slope * 1e6 if slope is not None else None
    return {
        "offset_seconds": offset_seconds,
        "drift_ppm": drift_ppm,
        "probe_count": len(samples),
    }


def _slope(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
