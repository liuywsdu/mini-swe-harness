"""纯逻辑单测（无需 VM / firecracker / Linux）。

- 动态冒烟（真机 firecracker）按用户要求不在本机执行；
- 真机验收命令见 README「H0 验收」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minisweharness.config import VMConfig
from minisweharness.lifecycle import REQUIRED_EVENTS, VMLifecycle
from minisweharness.timestamps import DualTimestamp


def test_lifecycle_records_dual_timestamps(tmp_path: Path):
    lifecycle = VMLifecycle(vm_id="vm-test", vmm="firecracker")
    lifecycle.mark("create_start", DualTimestamp(wall=100.0, monotonic=10.0))
    lifecycle.mark("ready", DualTimestamp(wall=103.5, monotonic=13.5))
    lifecycle.mark("destroy_confirmed", DualTimestamp(wall=110.0, monotonic=20.0))

    assert lifecycle.is_complete()
    assert lifecycle.missing_required() == []

    record = lifecycle.write(tmp_path / "vm_lifecycle.json")
    for name in REQUIRED_EVENTS:
        assert isinstance(record[name], float)  # host wall
        assert isinstance(record[f"{name}_monotonic"], float)

    on_disk = json.loads((tmp_path / "vm_lifecycle.json").read_text())
    assert on_disk["vm_id"] == "vm-test"
    assert on_disk["vmm"] == "firecracker"
    assert on_disk["create_start"] == 100.0
    assert on_disk["destroy_confirmed_monotonic"] == 20.0


def test_lifecycle_incomplete_detected():
    lifecycle = VMLifecycle(vm_id="vm-test", vmm="firecracker")
    lifecycle.mark("create_start")
    assert not lifecycle.is_complete()
    assert lifecycle.missing_required() == ["ready", "destroy_confirmed"]


def test_lifecycle_rejects_duplicate_event():
    lifecycle = VMLifecycle(vm_id="vm-test", vmm="firecracker")
    lifecycle.mark("create_start")
    with pytest.raises(ValueError, match="already recorded"):
        lifecycle.mark("create_start")


def test_vm_config_from_json_roundtrip(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "kernel_image": "/k/vmlinux",
                "root_drive": "/k/rootfs.ext4",
                "shared_mount": {"host_dir": "/srv/share"},
            }
        )
    )
    config = VMConfig.from_json(config_path)
    assert config.vcpu_count == 2  # 默认与 mini 对齐
    assert config.shared_mount is not None
    assert config.shared_mount.tag == "share"
    assert config.shared_mount.guest_path == "/mnt/share"


def test_vm_config_rejects_odd_vcpu():
    with pytest.raises(ValueError, match="1 or even"):
        VMConfig.from_dict({"kernel_image": "/k", "root_drive": "/r", "vcpu_count": 3})


def test_build_remote_command_local_helper():
    from minisweharness.vm import _build_remote_command

    remote = _build_remote_command("echo hi", "/root", {"A": "1 b"}, ["bash", "-lc"])
    assert remote.startswith("cd -- /root && export ")
    assert "bash -lc 'echo hi'" in remote
