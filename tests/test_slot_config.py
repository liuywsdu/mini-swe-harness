"""并发槽位网络派生测试：tap/guest_ip/guest_mac 三元组按槽位展开与越界拒绝。

hermetic：纯逻辑，不启动真实 VM、不触碰真实网络设备。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minisweharness.cli import main
from minisweharness.config import VMConfig, derive_slot_config


def _base() -> VMConfig:
    return VMConfig.from_dict({"kernel_image": "/k/vmlinux", "root_drive": "/k/rootfs.ext4"})


# ------------------------------------------------------------------ derive_slot_config


def test_slot_zero_matches_base_network_identity():
    base = _base()
    slot0 = derive_slot_config(base, 0)
    assert slot0.tap_device == base.tap_device
    assert slot0.guest_ip == base.guest_ip
    assert slot0.guest_mac == base.guest_mac.lower()  # MAC 大小写归一


def test_last_octet_mode_increments_last_ip_octet():
    derived = derive_slot_config(_base(), 3, ip_mode="last_octet")
    assert derived.tap_device == "tap3"
    assert derived.guest_ip == "172.16.0.5"  # 172.16.0.2 + 3
    assert derived.guest_mac == "06:00:ac:10:00:05"


def test_third_octet_mode_increments_third_ip_octet():
    derived = derive_slot_config(_base(), 3, ip_mode="third_octet")
    assert derived.tap_device == "tap3"
    assert derived.guest_ip == "172.16.3.2"
    assert derived.guest_mac == "06:00:ac:10:00:05"


def test_derived_pool_has_no_conflicts():
    pool = [derive_slot_config(_base(), slot) for slot in range(8)]
    assert len({c.tap_device for c in pool}) == 8
    assert len({c.guest_ip for c in pool}) == 8
    assert len({c.guest_mac for c in pool}) == 8


def test_last_octet_mode_rejects_crossing_slash24():
    with pytest.raises(ValueError, match="/24"):
        derive_slot_config(_base(), 300, ip_mode="last_octet")


def test_mac_overflow_rejected():
    base = VMConfig.from_dict(
        {
            "kernel_image": "/k",
            "root_drive": "/r",
            "guest_mac": "06:00:ac:10:00:ff",
        }
    )
    with pytest.raises(ValueError, match="guest_mac"):
        derive_slot_config(base, 1)


def test_negative_slot_rejected():
    with pytest.raises(ValueError, match="slot"):
        derive_slot_config(_base(), -1)


# ------------------------------------------------------------------ make-configs CLI


def test_make_configs_writes_slot_configs_and_host_commands(tmp_path: Path, capsys):
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps({"kernel_image": "/k", "root_drive": "/r"}))
    out_dir = tmp_path / "configs"
    rc = main(
        [
            "make-configs",
            "--base",
            str(base_path),
            "--slots",
            "3",
            "--ip-mode",
            "third_octet",
            "--out-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    for slot in range(3):
        config = VMConfig.from_json(out_dir / f"vm_config.slot{slot}.json")
        assert config.tap_device == f"tap{slot}"
        assert config.guest_ip == f"172.16.{slot}.2"
    stdout = capsys.readouterr().out
    assert "ip tuntap add tap0" in stdout
    assert "172.16.0.1/30" in stdout
    assert "172.16.1.1/30" in stdout
    assert "ip_forward=1" in stdout


def test_make_configs_last_octet_uses_bridge(tmp_path: Path, capsys):
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps({"kernel_image": "/k", "root_drive": "/r"}))
    rc = main(
        [
            "make-configs",
            "--base",
            str(base_path),
            "--slots",
            "2",
            "--out-dir",
            str(tmp_path / "configs"),
        ]
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "ip link add br0 type bridge" in stdout
    assert "172.16.0.1/24" in stdout
    assert "master br0" in stdout
