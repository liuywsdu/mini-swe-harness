"""双层 join + 对账（§5.3 H3 / §6 reconciliation.json）。

guest 事件（``guest_events.jsonl``，guest wall 时间戳）按
``host_wall = guest_wall − offset`` 映射到 host 时间线，再与 host 侧
``vm_samples.jsonl`` 在同一测量窗口（锚点 ``episode_start`` → ``episode_end``）
内分别汇总，产出比值（guest 合计 / host 总量，均 ≤ 1 为预期）。

口径说明（写入 ``notes``）：
- guest CPU 仅覆盖 action 窗口（``action_timing.resources.cpu_usage_usec``
  差分），等待期后台 CPU 由 host 采样整窗覆盖，故 cpu_ratio ≤ 1 为预期；
- 内存口径不同（guest cgroup ``memory.peak`` vs host VMM RSS peak），允许偏差；
- 网络取 guest 容器 netns ``session_snapshot.final.network_delta``，对 host tap
  计数；io 取 cgroup io.stat 差分，对 VMM ``/proc/<pid>/io`` 累计。
"""

from __future__ import annotations

import json
from pathlib import Path


def map_guest_to_host(events: list[dict], offset_seconds: float) -> list[dict]:
    """给 guest 事件附加 host 时间线（``host_wall_ts = wall_ts − offset``）。"""
    mapped = []
    for event in events:
        row = dict(event)
        wall_ts = event.get("wall_ts")
        row["host_wall_ts"] = wall_ts - offset_seconds if wall_ts is not None else None
        mapped.append(row)
    return mapped


def guest_totals(
    events: list[dict],
    *,
    offset_seconds: float,
    window_start_wall: float,
    window_end_wall: float,
) -> dict:
    """guest 层六指标合计（仅 action 窗口 + 最终 session_snapshot 网络）。"""
    cpu_usec = 0
    io_read = 0
    io_write = 0
    memory_peak: int | None = None
    net_rx: int | None = None
    net_tx: int | None = None
    action_count = 0
    for event in events:
        host_wall = (event.get("wall_ts") or 0.0) - offset_seconds
        if not (window_start_wall <= host_wall <= window_end_wall):
            continue
        payload = event.get("payload") or {}
        if event.get("kind") == "action_timing":
            resources = payload.get("resources") or {}
            cpu_usec += resources.get("cpu_usage_usec") or 0
            io_read += resources.get("io_read_bytes") or 0
            io_write += resources.get("io_write_bytes") or 0
            if resources.get("memory_peak_bytes") is not None:
                memory_peak = max(memory_peak or 0, resources["memory_peak_bytes"])
            action_count += 1
        elif event.get("kind") == "session_snapshot" and payload.get("final"):
            delta = payload.get("network_delta") or {}
            net_rx = delta.get("rx_bytes")
            net_tx = delta.get("tx_bytes")
    return {
        "cpu_usec": cpu_usec,
        "io_read_bytes": io_read,
        "io_write_bytes": io_write,
        "memory_peak_bytes": memory_peak,
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
        "action_count": action_count,
    }


def host_totals(
    vm_samples: list[dict],
    *,
    window_start_monotonic: float,
    window_end_monotonic: float,
) -> dict:
    """host/VM 层六指标合计：累计计数器首末差分，驻留取峰值。"""
    window = [
        sample
        for sample in vm_samples
        if window_start_monotonic <= sample["monotonic_ts"] <= window_end_monotonic
    ]
    if not window:
        return {}
    first, last = window[0], window[-1]

    def delta(field: str) -> int | None:
        before, after = first.get(field), last.get(field)
        return after - before if before is not None and after is not None else None

    return {
        "cpu_usec": delta("cpu_usec"),
        "io_read_bytes": delta("io_read_bytes"),
        "io_write_bytes": delta("io_write_bytes"),
        "net_rx_bytes": delta("net_rx_bytes"),
        "net_tx_bytes": delta("net_tx_bytes"),
        "rss_peak_bytes": max(
            (sample.get("rss_bytes") for sample in window if sample.get("rss_bytes") is not None),
            default=None,
        ),
        "sample_count": len(window),
    }


def reconcile(guest: dict, host: dict) -> dict:
    """``{cpu_ratio, mem_ratio, io_ratio, net_ratio, notes}``（§6）。"""

    def ratio(numerator: int | None, denominator: int | None) -> float | None:
        if numerator is None or denominator is None or denominator == 0:
            return None
        return numerator / denominator

    io_guest = _sum_or_none(guest.get("io_read_bytes"), guest.get("io_write_bytes"))
    io_host = _sum_or_none(host.get("io_read_bytes"), host.get("io_write_bytes"))
    net_guest = _sum_or_none(guest.get("net_rx_bytes"), guest.get("net_tx_bytes"))
    net_host = _sum_or_none(host.get("net_rx_bytes"), host.get("net_tx_bytes"))
    return {
        "cpu_ratio": ratio(guest.get("cpu_usec"), host.get("cpu_usec")),
        "mem_ratio": ratio(guest.get("memory_peak_bytes"), host.get("rss_peak_bytes")),
        "io_ratio": ratio(io_guest, io_host),
        "net_ratio": ratio(net_guest, net_host),
        "notes": [
            "guest CPU 仅覆盖 action 窗口；等待期后台 CPU 由 host 采样整窗覆盖",
            "内存口径不同（guest cgroup peak vs host VMM RSS peak），允许偏差",
            "io/net 比值按 (read+write)/(rx+tx) 汇总，避免单方向为零时的除零",
        ],
    }


def _sum_or_none(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left + right


def load_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
