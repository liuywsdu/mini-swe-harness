"""Firecracker API socket 的薄客户端（HTTP over unix socket，stdlib 实现）。

启动参数与镜像约定复用 mini-swe-agent ``executors/firecracker.py``：
machine-config → boot-source → drives/rootfs → network-interfaces →
(virtio-fs) → actions/InstanceStart。
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = Path(socket_path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


class FirecrackerAPI:
    def __init__(self, api_socket: Path, timeout: float = 5.0):
        self.api_socket = Path(api_socket)
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        connection = _UnixHTTPConnection(self.api_socket, self.timeout)
        body = json.dumps(payload) if payload is not None else None
        try:
            connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if not 200 <= response.status < 300:
            msg = f"firecracker API {method} {path} failed with HTTP {response.status}: {raw}"
            raise RuntimeError(msg)
        return json.loads(raw) if raw else None

    def put_machine_config(
        self,
        *,
        vcpu_count: int,
        mem_size_mib: int,
        smt: bool = False,
        track_dirty_pages: bool = False,
        cpu_template: str = "",
        huge_pages: str = "",
    ) -> None:
        payload: dict[str, Any] = {
            "vcpu_count": vcpu_count,
            "mem_size_mib": mem_size_mib,
            "smt": smt,
            "track_dirty_pages": track_dirty_pages,
        }
        if cpu_template:
            payload["cpu_template"] = cpu_template
        if huge_pages:
            payload["huge_pages"] = huge_pages
        self.request("PUT", "/machine-config", payload)

    def put_boot_source(self, kernel_image_path: Path, boot_args: str, initrd_path: Path | None = None) -> None:
        payload: dict[str, Any] = {"kernel_image_path": str(kernel_image_path), "boot_args": boot_args}
        if initrd_path is not None:
            payload["initrd_path"] = str(initrd_path)
        self.request("PUT", "/boot-source", payload)

    def put_root_drive(
        self,
        path_on_host: Path,
        drive_id: str = "rootfs",
        is_read_only: bool = False,
        cache_type: str = "Unsafe",
        io_engine: str = "Sync",
    ) -> None:
        self.request(
            "PUT",
            f"/drives/{drive_id}",
            {
                "drive_id": drive_id,
                "path_on_host": str(path_on_host),
                "is_root_device": True,
                "is_read_only": is_read_only,
                "cache_type": cache_type,
                "io_engine": io_engine,
            },
        )

    def put_network_interface(self, iface_id: str, host_dev_name: str, guest_mac: str) -> None:
        self.request(
            "PUT",
            f"/network-interfaces/{iface_id}",
            {"iface_id": iface_id, "host_dev_name": host_dev_name, "guest_mac": guest_mac},
        )

    def put_virtio_fs(self, tag: str, socket_path: Path, num_queues: int = 1, queue_size: int = 1024) -> None:
        """挂载 virtiofs 共享目录；socket 由宿主机 virtiofsd 提供。"""
        self.request(
            "PUT",
            "/virtio-fs",
            {
                "tag": tag,
                "socket": str(socket_path),
                "num_queues": num_queues,
                "queue_size": queue_size,
            },
        )

    def instance_start(self) -> None:
        self.request("PUT", "/actions", {"action_type": "InstanceStart"})

    def send_ctrl_alt_del(self) -> None:
        self.request("PUT", "/actions", {"action_type": "SendCtrlAltDel"})
