"""测量质量记录（§5.2 reconciliation / §5.3 H4 / §6 episode.json quality）。

把双层对账比值与阈值比较，超阈值偏差、缺失指标、coverage 事故写进
``quality``，供后续分析识别不可信数据点。阈值可覆盖；内存口径不同（guest
cgroup peak vs host VMM RSS peak），默认放宽。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityThresholds:
    cpu_ratio_max: float = 1.0
    io_ratio_max: float = 1.0
    net_ratio_max: float = 1.0
    mem_ratio_max: float = 1.5  # 内存口径不同，允许超 1
    coverage_gap_limit: int = 3


def evaluate_quality(
    reconciliation: dict,
    *,
    thresholds: QualityThresholds | None = None,
    coverage_gaps: int = 0,
) -> list[dict]:
    """reconciliation 比值 + coverage 事故 → quality 条目列表。"""
    t = thresholds or QualityThresholds()
    checks = [
        ("cpu_ratio", reconciliation.get("cpu_ratio"), t.cpu_ratio_max),
        ("io_ratio", reconciliation.get("io_ratio"), t.io_ratio_max),
        ("net_ratio", reconciliation.get("net_ratio"), t.net_ratio_max),
        ("mem_ratio", reconciliation.get("mem_ratio"), t.mem_ratio_max),
    ]
    entries: list[dict] = []
    for metric, value, limit in checks:
        if value is None:
            entries.append(
                {"metric": metric, "value": None, "limit": limit, "level": "info", "message": "metric unavailable"}
            )
        elif value > limit:
            entries.append(
                {
                    "metric": metric,
                    "value": value,
                    "limit": limit,
                    "level": "warn",
                    "message": f"{metric} exceeds threshold {limit}",
                }
            )
    if coverage_gaps > t.coverage_gap_limit:
        entries.append(
            {
                "metric": "coverage_gaps",
                "value": coverage_gaps,
                "limit": t.coverage_gap_limit,
                "level": "warn",
                "message": f"coverage gaps exceed {t.coverage_gap_limit}",
            }
        )
    return entries


def quality_record(
    entries: list[dict],
    *,
    baseline_profile_id: str | None = None,
    thresholds: QualityThresholds | None = None,
) -> dict:
    """§6 ``quality: {...}``：条目 + 基线引用 + 生效阈值。"""
    t = thresholds or QualityThresholds()
    return {
        "entries": entries,
        "baseline_profile_id": baseline_profile_id,
        "thresholds": {
            "cpu_ratio_max": t.cpu_ratio_max,
            "io_ratio_max": t.io_ratio_max,
            "net_ratio_max": t.net_ratio_max,
            "mem_ratio_max": t.mem_ratio_max,
            "coverage_gap_limit": t.coverage_gap_limit,
        },
    }
