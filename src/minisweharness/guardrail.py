"""宿主机 guardrail：并发上限 + 内存/磁盘下限（§5.1 / §5.3 H5）。

沿用 ``gpu_tasks/container.py`` 的 POLICY 思路：宿主机可用内存与磁盘可用空间
低于下限时暂停/拒绝新 VM 的接入，避免静默超载。读数可注入（测试用假 snapshot）。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuardrailPolicy:
    max_concurrency: int = 1
    host_available_memory_floor_gib: float = 12.0
    host_disk_free_floor_gib: float = 50.0
    queue_on_pressure: bool = True
    admission_poll_seconds: float = 1.0
    admission_timeout_seconds: float = 600.0


@dataclass(frozen=True)
class HostSnapshot:
    available_memory_gib: float | None
    disk_free_gib: float | None


def read_host_snapshot(
    disk_path: str | Path,
    *,
    meminfo_path: str | Path = "/proc/meminfo",
) -> HostSnapshot:
    """读宿主机可用内存（MemAvailable）与磁盘可用空间；不支持读记 None。"""
    available_memory = None
    meminfo = Path(meminfo_path)
    if meminfo.is_file():
        for line in meminfo.read_text(errors="replace").splitlines():
            if line.startswith("MemAvailable:"):
                available_memory = int(line.split()[1]) * 1024 / 2**30
                break
    disk_free = None
    try:
        disk_free = shutil.disk_usage(Path(disk_path)).free / 2**30
    except OSError:
        disk_free = None
    return HostSnapshot(available_memory_gib=available_memory, disk_free_gib=disk_free)


def admit(snapshot: HostSnapshot, policy: GuardrailPolicy) -> tuple[bool, str]:
    """接入判定：返回 (是否放行, 不放行原因)。"""
    memory = snapshot.available_memory_gib
    if memory is not None and memory < policy.host_available_memory_floor_gib:
        return False, f"host available memory {memory:.2f}GiB below floor {policy.host_available_memory_floor_gib}GiB"
    disk = snapshot.disk_free_gib
    if disk is not None and disk < policy.host_disk_free_floor_gib:
        return False, f"host disk free {disk:.2f}GiB below floor {policy.host_disk_free_floor_gib}GiB"
    return True, ""
