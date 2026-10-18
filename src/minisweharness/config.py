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
