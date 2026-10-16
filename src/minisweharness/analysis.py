"""vm_samples 分析辅助：窗口差分与 memory-time 梯形积分。

口径（计划文档 §5.1）：``cpu_usec``/``io_*`` 是累计计数器，窗口消耗 =
相邻样本差分；``rss_bytes``/``pss_bytes`` 是瞬时值，memory-time =
样本间梯形积分（``∫ mem dt``，单位 byte·s）。
"""

from __future__ import annotations

from typing import Iterable

_COUNTER_FIELDS = ("cpu_usec", "io_read_bytes", "io_write_bytes", "net_rx_bytes", "net_tx_bytes")


def window_deltas(samples: list[dict]) -> list[dict]:
    """相邻样本的计数器差分（monotonic 窗口）；首个样本无窗口，跳过。"""
    deltas = []
    for previous, current in zip(samples, samples[1:]):
        window = current["monotonic_ts"] - previous["monotonic_ts"]
        delta = {"window_seconds": window, "start_monotonic": previous["monotonic_ts"]}
        for field in _COUNTER_FIELDS:
            before, after = previous.get(field), current.get(field)
            delta[field] = None if before is None or after is None else after - before
        deltas.append(delta)
    return deltas


def memory_time(samples: list[dict], field: str = "rss_bytes") -> float | None:
    """瞬存字段的梯形积分（byte·s）；任一样本为 null 则整体记 null。"""
    total = 0.0
    for previous, current in zip(samples, samples[1:]):
        before, after = previous.get(field), current.get(field)
        if before is None or after is None:
            return None
        total += (before + after) / 2 * (current["monotonic_ts"] - previous["monotonic_ts"])
    return total


def coverage_gaps(samples: Iterable[dict], limit: int = 3) -> list[dict]:
    """coverage_gap 超阈值的样本（漏采 > 3 周期标 coverage 事故）。"""
    return [s for s in samples if s.get("coverage_gap", 0) > limit]
