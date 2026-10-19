"""harness 配置（JSON 文件加载，stdlib dataclass，无第三方依赖）。

字段命名与默认值对齐 mini-swe-agent ``executors/ssh_vm.py`` /
``executors/firecracker.py`` 的约定（guest_ip/tap/guest_mac/boot_args 等）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SharedMountConfig:
    """宿主机与 guest 的共享目录约定。

    默认 ``ssh`` 后端通过 SCP 显式上传/下载，不要求 VMM 支持共享文件系统；
    ``virtiofs`` 后端保留给实现了相应 API 的 VMM。
    """

    host_dir: str
    """宿主机侧共享目录（harness 写、guest 读写）。"""

    guest_path: str = "/mnt/share"
    backend: str = "ssh"
    """共享目录传输后端：``ssh``（默认）或 ``virtiofs``。"""

    tag: str = "share"
    virtiofsd_executable: str = "virtiofsd"
    virtiofsd_args: list[str] = field(default_factory=lambda: ["-o", "cache=auto"])

    def __post_init__(self) -> None:
        if self.backend not in ("ssh", "virtiofs"):
            raise ValueError("shared_mount.backend must be 'ssh' or 'virtiofs'")


@dataclass(frozen=True)
class VMConfig:
    kernel_image: str
    root_drive: str
    firecracker_executable: str = "firecracker"
    vcpu_count: int = 2
    mem_size_mib: int = 2048
    smt: bool = False
    track_dirty_pages: bool = False
    cpu_template: str = ""
    huge_pages: str = ""
    boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off"
    initrd_path: str = ""
    root_read_only: bool = False
    copy_root_drive: bool = True
    guest_ip: str = "172.16.0.2"
    tap_device: str = "tap0"
    guest_mac: str = "06:00:AC:10:00:02"
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_identity_file: str = ""
    ssh_args: list[str] = field(
        default_factory=lambda: [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=2",
        ]
    )
    startup_timeout_seconds: float = 120.0
    api_timeout_seconds: float = 5.0
    command_timeout_seconds: float = 30.0
    shared_mount: SharedMountConfig | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "VMConfig":
        data = dict(data)
        shared = data.pop("shared_mount", None)
        config = cls(**data)
        if shared is not None:
            config = cls(**{**config.__dict__, "shared_mount": SharedMountConfig(**shared)})
        if config.vcpu_count != 1 and config.vcpu_count % 2:
            raise ValueError("vcpu_count must be 1 or even")
        return config

    @classmethod
    def from_json(cls, path: str | Path) -> "VMConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))

# ------------------------------------------------------------------ 并发槽位网络派生


def _derive_tap_name(base_tap: str, slot: int) -> str:
    stem = base_tap.rstrip("0123456789")
    return f"{stem}{slot}"


def _derive_guest_ip(base_ip: str, slot: int, ip_mode: str) -> str:
    import ipaddress

    ip = int(ipaddress.IPv4Address(base_ip))
    if ip_mode == "last_octet":
        derived = ip + slot
        if derived & ~0xFF != ip & ~0xFF:
            raise ValueError(
                f"guest_ip {base_ip} + slot {slot} crosses the /24 boundary; "
                "use ip_mode='third_octet' or a smaller concurrency"
            )
    elif ip_mode == "third_octet":
        derived = ip + slot * 256
        if derived >> 16 != ip >> 16:
            raise ValueError(f"guest_ip {base_ip} + slot {slot} crosses the second octet")
    else:
        raise ValueError(f"unknown ip_mode: {ip_mode}")
    if derived & 0xFF in (0, 255):
        raise ValueError(f"derived guest_ip ends in .0/.255: {ipaddress.IPv4Address(derived)}")
    return str(ipaddress.IPv4Address(derived))


def _derive_guest_mac(base_mac: str, slot: int) -> str:
    octets = [o.lower() for o in base_mac.split(":")]
    if len(octets) != 6:
        raise ValueError(f"guest_mac must have 6 octets: {base_mac}")
    last = int(octets[-1], 16) + slot
    if last > 0xFF:
        raise ValueError(f"guest_mac {base_mac} + slot {slot} overflows the last octet")
    octets[-1] = f"{last:02x}"
    return ":".join(octets)


def derive_slot_config(base: VMConfig, slot: int, *, ip_mode: str = "last_octet") -> VMConfig:
    """派生第 ``slot`` 个并发槽位的 VM 配置（base 视为 slot 0）。

    每个并发 VM 必须独占 ``tap_device``/``guest_ip``/``guest_mac`` 三元组，
    否则 firecracker 网络接口互相冲突（一个 tap 只能挂一个 VMM）。本函数按
    槽位序号派生三元组：

    - ``tap_device``：去掉 base 名尾数字追加槽位（``tap0`` → ``tap{i}``）；
    - ``guest_ip``：``ip_mode="last_octet"``（默认，宿主机用 bridge 汇聚所有
      tap 成一个 /24）末字节加槽位；``ip_mode="third_octet"``（每 tap 一个独立
      /30，如 firecracker getting-started）第三字节加槽位；
    - ``guest_mac``：末字节加槽位（溢出报错）。

    派生越界（/24 或 MAC 溢出）在起跑前抛 ``ValueError``，绝不静默生成冲突配置。
    """
    if slot < 0:
        raise ValueError(f"slot must be >= 0: {slot}")
    data = dict(base.__dict__)
    if data["shared_mount"] is not None:
        data["shared_mount"] = dict(data["shared_mount"].__dict__)
    return VMConfig.from_dict(
        {
            **data,
            "tap_device": _derive_tap_name(base.tap_device, slot),
            "guest_ip": _derive_guest_ip(base.guest_ip, slot, ip_mode),
            "guest_mac": _derive_guest_mac(base.guest_mac, slot),
        }
    )
