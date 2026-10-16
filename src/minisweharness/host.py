"""宿主机信息（§6 run_manifest.host）与 run_manifest 默认形状。"""

from __future__ import annotations

import os
import platform
from pathlib import Path


def host_info() -> dict:
    """``{uname, cpu_model, mem_bytes}``（不支持读记 null）。"""

    def _cpu_model() -> str | None:
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.is_file():
            for line in cpuinfo.read_text(errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
            return None
        return platform.processor() or None

    mem_bytes = None
    try:
        mem_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        pass
    uname = platform.uname()
    return {
        "uname": f"{uname.system} {uname.release} {uname.machine}",
        "cpu_model": _cpu_model(),
        "mem_bytes": mem_bytes,
    }
