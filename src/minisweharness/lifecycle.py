"""VM 生命周期时间戳记录，产出计划文档 §6 规范的 ``vm_lifecycle.json``。

H0 验收口径：``create_start`` / ``ready`` / ``destroy_confirmed`` 三个
host wall 时间戳齐全；另按横向前提①附 monotonic 伴侣字段。
"""

from __future__ import annotations

import json
from pathlib import Path

from minisweharness.timestamps import DualTimestamp, now

REQUIRED_EVENTS = ("create_start", "ready", "destroy_confirmed")


class VMLifecycle:
    """按事件名累计时间戳；导出为 §6 规范的 vm_lifecycle.json。"""

    def __init__(self, vm_id: str, vmm: str):
        self.vm_id = vm_id
        self.vmm = vmm
        self._events: dict[str, DualTimestamp] = {}

    def mark(self, name: str, timestamp: DualTimestamp | None = None) -> DualTimestamp:
        if name in self._events:
            raise ValueError(f"lifecycle event already recorded: {name}")
        timestamp = timestamp or now()
        self._events[name] = timestamp
        return timestamp

    def get(self, name: str) -> DualTimestamp | None:
        return self._events.get(name)

    def missing_required(self) -> list[str]:
        return [name for name in REQUIRED_EVENTS if name not in self._events]

    def is_complete(self) -> bool:
        return not self.missing_required()

    def to_dict(self) -> dict:
        record: dict = {"vm_id": self.vm_id, "vmm": self.vmm}
        for name, ts in self._events.items():
            record[name] = ts.wall
            record[f"{name}_monotonic"] = ts.monotonic
        phases = {
            name: {"wall": ts.wall, "monotonic": ts.monotonic}
            for name, ts in self._events.items()
            if name not in REQUIRED_EVENTS
        }
        if phases:
            record["phases"] = phases
        return record

    def write(self, path: Path) -> dict:
        record = self.to_dict()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        return record
