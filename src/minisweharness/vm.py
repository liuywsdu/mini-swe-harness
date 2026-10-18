"""单 VM 生命周期管理：创建（firecracker + 共享目录）→ 就绪 → 销毁。

H0 冒烟：guest 内 hello-world + 共享挂载双向校验，产出
``vm_lifecycle.json``（验收：生命周期时间戳齐全）与 ``smoke.json``。
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from minisweharness.config import VMConfig
from minisweharness.firecracker import FirecrackerAPI
from minisweharness.lifecycle import VMLifecycle
from minisweharness.timestamps import now

logger = logging.getLogger("minisweharness.vm")


def _build_remote_command(command: str, cwd: str, env: dict[str, str], interpreter: list[str]) -> str:
    """与 mini-swe-agent ``executors/ssh_vm.py`` 相同的远端命令组装约定。"""
    setup = [f"cd -- {shlex.quote(cwd)}"]
    setup.extend(f"export {shlex.quote(f'{key}={value}')}" for key, value in env.items())
    setup.append(f"{shlex.join(interpreter)} {shlex.quote(command)}")
    return " && ".join(setup)


class GuestCommandError(RuntimeError):
    def __init__(self, command: str, returncode: int, output: str):
        super().__init__(f"guest command failed (exit {returncode}): {command}\n{output[-2000:]}")
        self.returncode = returncode
        self.output = output


class SingleVM:
    """一台 firecracker microVM 的完整生命周期（H0 范围）。"""

    def __init__(self, config: VMConfig, vm_id: str | None = None, keep_runtime_dir: bool = False):
        self.config = config
        self.vm_id = vm_id or f"h0-{uuid.uuid4().hex[:12]}"
        self.lifecycle = VMLifecycle(vm_id=self.vm_id, vmm="firecracker")
        self.keep_runtime_dir = keep_runtime_dir
        self.runtime_dir = Path(tempfile.mkdtemp(prefix=f"minisweharness-{self.vm_id}-"))
        self.api_socket = self.runtime_dir / "api.socket"
        self.virtiofsd_socket = self.runtime_dir / "virtiofsd.sock"
        self.log_path = self.runtime_dir / "firecracker.log"
        self._log_stream = None
        self.process: subprocess.Popen | None = None
        self.virtiofsd_process: subprocess.Popen | None = None
        self.api = FirecrackerAPI(self.api_socket, timeout=config.api_timeout_seconds)

    # ------------------------------------------------------------------ 创建

    def create(self) -> None:
        """create_start → API 配置 → InstanceStart → SSH 就绪 → 共享挂载就绪 → ready。"""
        self.lifecycle.mark("create_start")
        executable = self._validate_host()
        kernel = self._resolve_file(self.config.kernel_image, "Kernel image")
        initrd = self._resolve_file(self.config.initrd_path, "Initrd") if self.config.initrd_path else None
        root_drive = self._prepare_root_drive()

        if self.config.shared_mount is not None and self.config.shared_mount.backend == "virtiofs":
            self._start_virtiofsd(Path(self.config.shared_mount.host_dir).resolve())

        self._launch([executable, "--api-sock", str(self.api_socket)])
        self.api.put_machine_config(
            vcpu_count=self.config.vcpu_count,
            mem_size_mib=self.config.mem_size_mib,
            smt=self.config.smt,
            track_dirty_pages=self.config.track_dirty_pages,
            cpu_template=self.config.cpu_template,
            huge_pages=self.config.huge_pages,
        )
        self.api.put_boot_source(kernel, self.config.boot_args, initrd)
        self.api.put_root_drive(
            root_drive,
            is_read_only=self.config.root_read_only,
        )
        self.api.put_network_interface("eth0", self.config.tap_device, self.config.guest_mac)
        if self.config.shared_mount is not None and self.config.shared_mount.backend == "virtiofs":
            self.api.put_virtio_fs(self.config.shared_mount.tag, self.virtiofsd_socket)
        self.lifecycle.mark("api_configured")
        self.api.instance_start()
        self.lifecycle.mark("instance_start_sent")
        self._wait_for_ssh()
        self.lifecycle.mark("ssh_ready")
        if self.config.shared_mount is not None:
            self._setup_shared_mount()
            self.lifecycle.mark("mount_ready")
        self.lifecycle.mark("ready")
        logger.info("VM %s ready", self.vm_id)

    def _validate_host(self) -> str:
        executable = shutil.which(self.config.firecracker_executable)
        if executable is None:
            raise FileNotFoundError(f"Executable not found: {self.config.firecracker_executable}")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise PermissionError("firecracker requires read/write access to /dev/kvm")
        if not (Path("/sys/class/net") / self.config.tap_device).exists():
            raise FileNotFoundError(f"TAP device not found: {self.config.tap_device}")
        if self.config.shared_mount is not None:
            host_dir = Path(self.config.shared_mount.host_dir)
            if not host_dir.is_dir():
                raise FileNotFoundError(f"Shared mount host dir not found: {host_dir}")
            if self.config.shared_mount.backend == "ssh":
                if shutil.which("scp") is None:
                    raise FileNotFoundError("Executable not found: scp (required by shared_mount.backend=ssh)")
            elif shutil.which(self.config.shared_mount.virtiofsd_executable) is None:
                raise FileNotFoundError(
                    f"Executable not found: {self.config.shared_mount.virtiofsd_executable}"
                )
        if self.config.ssh_identity_file:
            self._resolve_file(self.config.ssh_identity_file, "SSH identity file")
        return executable

    def _resolve_file(self, value: str, label: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
        return path

    def _prepare_root_drive(self) -> Path:
        source = self._resolve_file(self.config.root_drive, "Root drive")
        if not self.config.copy_root_drive:
            return source
        destination = self.runtime_dir / source.name
        subprocess.run(
            ["cp", "--reflink=auto", "--sparse=always", "--", str(source), str(destination)],
            check=True,
            capture_output=True,
            text=True,
        )
        return destination

    def _start_virtiofsd(self, shared_dir: Path) -> None:
        shared = self.config.shared_mount
        command = [
            shared.virtiofsd_executable,
            f"--socket-path={self.virtiofsd_socket}",
            f"--shared-dir={shared_dir}",
            *shared.virtiofsd_args,
        ]
        self.virtiofsd_process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _launch(self, command: list[str]) -> None:
        self._log_stream = self.log_path.open("w")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self._log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._wait_for_api_socket()

    def _wait_for_api_socket(self) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"firecracker exited before opening its API socket:\n{self._log_tail()}")
            if self.api_socket.exists():
                return
            time.sleep(0.01)
        raise TimeoutError(f"Timed out waiting for firecracker API socket:\n{self._log_tail()}")

    # ------------------------------------------------------------------ SSH

    def _ssh_command(self, remote_command: str) -> list[str]:
        command = [
            "ssh",
            "-T",
            "-p",
            str(self.config.ssh_port),
            "-o",
            f"UserKnownHostsFile={self.runtime_dir / 'known_hosts'}",
            *self.config.ssh_args,
        ]
        if self.config.ssh_identity_file:
            command.extend(["-i", str(Path(self.config.ssh_identity_file).expanduser().resolve())])
        command.extend([f"{self.config.ssh_user}@{self.config.guest_ip}", remote_command])
        return command

    def guest_exec(self, command: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        """经 SSH 在 guest 内执行命令；超时按 SSH 连接超时处理。"""
        remote = _build_remote_command(command, "/root", {}, ["bash", "-lc"])
        return subprocess.run(
            self._ssh_command(remote),
            text=True,
            capture_output=True,
            timeout=timeout or self.config.command_timeout_seconds,
        )

    def guest_check(self, command: str, timeout: float | None = None) -> str:
        result = self.guest_exec(command, timeout)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise GuestCommandError(command, result.returncode, output)
        return result.stdout.strip()

    # -------------------------------------------------------- SSH 文件传输

    @staticmethod
    def _validate_guest_path(value: str | Path) -> str:
        path = PurePosixPath(str(value))
        if not path.is_absolute() or str(path) == "/" or "\n" in str(path):
            raise ValueError(f"guest path must be an absolute non-root path: {value!s}")
        return str(path)

    def _remote_spec(self, guest_path: str | Path) -> str:
        path = self._validate_guest_path(guest_path)
        return f"{self.config.ssh_user}@{self.config.guest_ip}:{shlex.quote(path)}"

    def _scp_command(self, *extra_args: str) -> list[str]:
        command = [
            "scp",
            "-O",  # 强制传统 SCP 协议；不依赖 guest sshd 的 SFTP subsystem
            "-p",
            "-P",
            str(self.config.ssh_port),
            "-o",
            f"UserKnownHostsFile={self.runtime_dir / 'known_hosts'}",
            *self.config.ssh_args,
        ]
        if self.config.ssh_identity_file:
            command.extend(["-i", str(Path(self.config.ssh_identity_file).expanduser().resolve())])
        command.extend(extra_args)
        return command

    def _run_scp(self, *extra_args: str, timeout: float | None = None) -> None:
        command = self._scp_command(*extra_args)
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout or self.config.command_timeout_seconds,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            raise GuestCommandError(shlex.join(command), result.returncode, output)

    def upload_file(self, local_path: str | Path, guest_path: str | Path, timeout: float | None = None) -> None:
        """通过 SCP 上传单个文件；guest 父目录自动创建。"""
        source = self._resolve_file(str(local_path), "Upload file")
        destination = self._validate_guest_path(guest_path)
        parent = str(PurePosixPath(destination).parent)
        self.guest_check(f"mkdir -p {shlex.quote(parent)}", timeout)
        self._run_scp(str(source), self._remote_spec(destination), timeout=timeout)

    def download_file(self, guest_path: str | Path, local_path: str | Path, timeout: float | None = None) -> None:
        """通过 SCP 下载单个文件；宿主机父目录自动创建。"""
        source = self._validate_guest_path(guest_path)
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run_scp(self._remote_spec(source), str(destination), timeout=timeout)

    def upload_directory(self, local_dir: str | Path, guest_dir: str | Path, timeout: float | None = None) -> None:
        """通过 ``scp -r`` 上传目录，目标路径保持为 ``guest_dir`` 本身。"""
        source = Path(local_dir).expanduser().resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"Upload directory not found: {source}")
        destination = self._validate_guest_path(guest_dir)
        parent = str(PurePosixPath(destination).parent)
        self.guest_check(
            f"rm -rf -- {shlex.quote(destination)} && mkdir -p {shlex.quote(parent)}",
            timeout,
        )
        self._run_scp("-r", str(source), self._remote_spec(destination), timeout=timeout)

    def download_directory(self, guest_dir: str | Path, local_dir: str | Path, timeout: float | None = None) -> None:
        """通过 ``scp -r`` 下载目录，目标路径保持为 ``local_dir`` 本身。"""
        source = self._validate_guest_path(guest_dir)
        destination = Path(local_dir)
        if destination.exists() or destination.is_symlink():
            if destination == Path(destination.anchor):
                raise ValueError(f"refusing to replace filesystem root: {destination}")
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run_scp("-r", self._remote_spec(source), str(destination), timeout=timeout)

    def read_guest_file(self, guest_path: str | Path, timeout: float | None = None) -> str | None:
        """读取 guest 小文本文件；不存在/不可读时返回 None（供心跳探针使用）。"""
        path = self._validate_guest_path(guest_path)
        try:
            result = self.guest_exec(f"cat -- {shlex.quote(path)}", timeout)
        except subprocess.TimeoutExpired:
            return None
        return result.stdout if result.returncode == 0 else None

    def _wait_for_ssh(self) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"firecracker exited while waiting for SSH:\n{self._log_tail()}")
            try:
                result = subprocess.run(
                    self._ssh_command("true"),
                    text=True,
                    capture_output=True,
                    timeout=min(3, max(0.1, deadline - time.monotonic())),
                )
                if result.returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for SSH at {self.config.guest_ip}:\n{self._log_tail()}")

    # -------------------------------------------------------------- 共享挂载

    def _setup_shared_mount(self) -> None:
        shared = self.config.shared_mount
        guest_path = shared.guest_path
        self.guest_check(f"mkdir -p {shlex.quote(guest_path)}")
        if shared.backend == "ssh":
            return
        mounted = self.guest_exec(f"mountpoint -q {shlex.quote(guest_path)}")
        if mounted.returncode != 0:
            self.guest_check(f"mount -t virtiofs {shlex.quote(shared.tag)} {shlex.quote(guest_path)}")

    # ------------------------------------------------------------ H0 冒烟

    def hello_world(self, shared_dir: Path) -> dict:
        """guest 内 hello-world + 共享挂载双向校验，返回 smoke 结果字典。"""
        hello = self.guest_check("echo hello-world && uname -a")
        result: dict = {
            "vm_id": self.vm_id,
            "hello_world": {"command": "echo hello-world && uname -a", "output": hello},
            "shared_mount": None,
        }
        if self.config.shared_mount is not None:
            shared = self.config.shared_mount
            probe_name = f"_harness_h0_{self.vm_id}.txt"
            host_probe = shared_dir / probe_name
            host_probe.write_text(f"hello from host {self.vm_id}\n")
            try:
                guest_probe = f"{shared.guest_path}/{probe_name}"
                if shared.backend == "ssh":
                    self.upload_file(host_probe, guest_probe)
                guest_read = self.guest_check(f"cat {shlex.quote(guest_probe)}")
                host_to_guest_ok = guest_read.strip() == host_probe.read_text().strip()
                reply_name = f"_harness_h0_{self.vm_id}.reply.txt"
                guest_reply_path = f"{shared.guest_path}/{reply_name}"
                self.guest_check(f"echo hello from guest > {shlex.quote(guest_reply_path)}")
                if shared.backend == "ssh":
                    self.download_file(guest_reply_path, shared_dir / reply_name)
                guest_reply = (shared_dir / reply_name).read_text().strip()
                guest_to_host_ok = guest_reply == "hello from guest"
            finally:
                host_probe.unlink(missing_ok=True)
                (shared_dir / f"_harness_h0_{self.vm_id}.reply.txt").unlink(missing_ok=True)
            result["shared_mount"] = {
                "backend": shared.backend,
                "transport": "scp" if shared.backend == "ssh" else "virtiofs",
                "tag": shared.tag,
                "host_dir": str(shared_dir),
                "guest_path": shared.guest_path,
                "host_to_guest_ok": host_to_guest_ok,
                "guest_to_host_ok": guest_to_host_ok,
            }
            if not (host_to_guest_ok and guest_to_host_ok):
                raise RuntimeError(f"shared mount bidirectional check failed: {result['shared_mount']}")
        return result

    # ------------------------------------------------------------------ 销毁

    def destroy(self) -> None:
        """优雅关停（SendCtrlAltDel）→ 进程回收确认 → destroy_confirmed。"""
        process = self.process
        if process is not None and process.poll() is None:
            try:
                self.api.send_ctrl_alt_del()
                process.wait(timeout=5)
            except Exception:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        virtiofsd = self.virtiofsd_process
        if virtiofsd is not None and virtiofsd.poll() is None:
            virtiofsd.terminate()
            try:
                virtiofsd.wait(timeout=2)
            except subprocess.TimeoutExpired:
                virtiofsd.kill()
                virtiofsd.wait()
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
        if self.runtime_dir.exists() and not self.keep_runtime_dir:
            shutil.rmtree(self.runtime_dir)
        self.lifecycle.mark("destroy_confirmed")
        logger.info("VM %s destroyed", self.vm_id)

    # ------------------------------------------------------------------ 其他

    def _log_tail(self) -> str:
        if self._log_stream is not None:
            self._log_stream.flush()
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")[-4000:]

    def __enter__(self) -> "SingleVM":
        self.create()
        return self

    def __exit__(self, *exc_info) -> None:
        self.destroy()


def run_h0_smoke(config: VMConfig, run_dir: Path, *, keep_runtime_dir: bool = False) -> dict:
    """H0 全流程：创建 → guest hello-world + 共享挂载校验 → 销毁 → 落盘。

    返回 ``vm_lifecycle.json`` 的记录字典；时间戳不齐（验收失败）时抛错。
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    vm = SingleVM(config, keep_runtime_dir=keep_runtime_dir)
    try:
        vm.create()
        shared_dir = (
            Path(config.shared_mount.host_dir).resolve() if config.shared_mount is not None else run_dir
        )
        smoke = vm.hello_world(shared_dir)
        vm.lifecycle.mark("smoke_done")
    finally:
        vm.destroy()
    record = vm.lifecycle.write(run_dir / "vm_lifecycle.json")
    import json

    (run_dir / "smoke.json").write_text(json.dumps(smoke, indent=2, ensure_ascii=False) + "\n")
    missing = vm.lifecycle.missing_required()
    if missing:
        raise RuntimeError(f"vm_lifecycle timestamps incomplete: {missing}")
    return record
