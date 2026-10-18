"""SSH/SCP 共享目录后端测试（全部 hermetic，不启动 VM/SSH）。"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from minisweharness.config import SharedMountConfig, VMConfig
from minisweharness.vm import SingleVM


def _config(tmp_path: Path, **shared_overrides) -> VMConfig:
    shared = {"host_dir": str(tmp_path), **shared_overrides}
    return VMConfig.from_dict(
        {
            "kernel_image": "/k/vmlinux-bz",
            "root_drive": "/k/rootfs.ext4",
            "shared_mount": shared,
        }
    )


def test_ssh_is_default_backend_and_invalid_backend_rejected(tmp_path: Path):
    config = _config(tmp_path)
    assert config.shared_mount.backend == "ssh"
    with pytest.raises(ValueError, match="backend"):
        SharedMountConfig(host_dir=str(tmp_path), backend="nfs")


def test_create_ssh_backend_never_calls_virtio_fs_api(tmp_path: Path):
    vm = SingleVM(_config(tmp_path), vm_id="ssh-create")

    class FakeAPI:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def call(*args, **kwargs):
                self.calls.append(name)

            return call

    api = FakeAPI()
    vm.api = api
    vm._validate_host = lambda: "/bin/true"
    vm._resolve_file = lambda value, label: Path(value)
    vm._prepare_root_drive = lambda: Path("/rootfs")
    vm._launch = lambda command: None
    vm._wait_for_ssh = lambda: None
    vm._setup_shared_mount = lambda: None
    vm._start_virtiofsd = lambda path: (_ for _ in ()).throw(AssertionError("virtiofsd must not start"))

    vm.create()

    assert "put_virtio_fs" not in api.calls
    assert "put_machine_config" in api.calls
    assert "put_boot_source" in api.calls
    assert "put_root_drive" in api.calls
    assert "put_network_interface" in api.calls
    assert "instance_start" in api.calls
    vm.destroy()


def test_h0_ssh_bidirectional_transfer(tmp_path: Path):
    vm = SingleVM(_config(tmp_path), vm_id="ssh-h0")
    remote_files: dict[str, str] = {}

    def upload_file(local_path, guest_path, timeout=None):
        remote_files[str(guest_path)] = Path(local_path).read_text()

    def download_file(guest_path, local_path, timeout=None):
        Path(local_path).write_text(remote_files[str(guest_path)])

    def guest_check(command, timeout=None):
        if command == "echo hello-world && uname -a":
            return "hello-world\nLinux fake-guest"
        if command.startswith("cat "):
            path = shlex.split(command)[1]
            return remote_files[path]
        if command.startswith("echo hello from guest > "):
            path = shlex.split(command)[-1]
            remote_files[path] = "hello from guest\n"
            return ""
        raise AssertionError(command)

    vm.upload_file = upload_file
    vm.download_file = download_file
    vm.guest_check = guest_check

    result = vm.hello_world(tmp_path)

    assert result["shared_mount"]["backend"] == "ssh"
    assert result["shared_mount"]["transport"] == "scp"
    assert result["shared_mount"]["host_to_guest_ok"] is True
    assert result["shared_mount"]["guest_to_host_ok"] is True
    assert not list(tmp_path.glob("_harness_h0_*"))
    vm.destroy()


def test_upload_file_uses_scp_with_guest_parent_setup(tmp_path: Path):
    vm = SingleVM(_config(tmp_path), vm_id="scp-command")
    source = tmp_path / "source.txt"
    source.write_text("payload")
    guest_commands = []
    scp_calls = []
    vm.guest_check = lambda command, timeout=None: guest_commands.append(command) or ""
    vm._run_scp = lambda *args, timeout=None: scp_calls.append(args)

    vm.upload_file(source, "/mnt/share/file with space.txt")

    assert guest_commands == ["mkdir -p /mnt/share"]
    assert scp_calls[0][0] == str(source.resolve())
    assert scp_calls[0][1] == "root@172.16.0.2:'/mnt/share/file with space.txt'"
    vm.destroy()


def test_scp_uses_legacy_protocol_without_sftp_subsystem(tmp_path: Path):
    vm = SingleVM(_config(tmp_path), vm_id="scp-protocol")
    command = vm._scp_command("source", "destination")
    assert command[:2] == ["scp", "-O"]
    vm.destroy()


def test_single_vm_accepts_baseline_factory_positional_args(tmp_path: Path):
    # baseline/run-episode 的 VMFactory 约定：(config, vm_id, keep_runtime_dir)
    vm = SingleVM(_config(tmp_path), "positional-vm", False)
    assert vm.vm_id == "positional-vm"
    assert vm.keep_runtime_dir is False
    vm.destroy()


def test_docker_login_password_uses_stdin_not_command_line(tmp_path: Path):
    vm = SingleVM(_config(tmp_path), vm_id="docker-auth")
    captured = {}

    def fake_exec(command, timeout=None, *, input_text=None):
        captured["command"] = command
        captured["input_text"] = input_text
        return subprocess.CompletedProcess(command, 0, "", "")

    vm.guest_exec = fake_exec
    vm.docker_login("harbor.private:12443", "robot$h2", "secret")

    assert captured["input_text"] == "secret\n"
    assert "--password-stdin" in captured["command"]
    assert "secret" not in captured["command"]
    vm.destroy()
