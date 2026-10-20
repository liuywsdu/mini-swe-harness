"""H2：episode 注入 + 镜像预热 + mini replay 单 task 运行器。

流程（计划文档 §5.3 H2 / §5.4 主方案）：

1. 起跑前：映射表覆盖校验（缺失/零 tag 报错；多 tag 标 skipped 不进 VM）；
   生成 override（image 直写 Harbor 全路径，字段白名单翻译）；
2. 备 VM（H0 SingleVM + SSH/SCP 共享目录 = episode 目录下 ``shared/``）；
3. 挂 VMM 采样器（H1）→ ``vm_samples.jsonl``；
4. 镜像预热（warm-up 阶段）：guest 内 ``docker pull <Harbor 全路径>``
   （override 名即 Harbor 名，无需 tag）并 inspect 验证；
5. episode 注入：源 store/override 经 SCP 上传，guest 内
   ``python -m minisweagent.run.replay run --environment-config <override>``（mini 对
   docker executor 强制 ``--pull=never``，命中本地 Harbor 名镜像）；guest 未预装
   mini-swe-agent 时传 ``mini_agent_dir``（含 minisweagent 包的目录，即 mini 仓库的
   ``src/``），先 SCP 上传再 guest 内 ``pip install``；
6. 收尾校验：``session_snapshot.final`` 存在、导出 JSONL 行数与 store 一致；
7. 落盘 §6 布局：episode.json / guest_events.jsonl / vm_samples.jsonl /
   vm_lifecycle.json / anchors.jsonl，run 根目录写 run_manifest.json（含
   ``image_map``，起跑前冻结）。

锚点切窗（§5.4 计量口径）：warmup_start/warmup_done 与
episode_start/episode_end 均为 host 双时间戳，pull 流量落在 warm-up 窗口，
不进 episode 测量窗口。
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from minisweharness.anchors import AnchorRecorder
from minisweharness.analysis import coverage_gaps
from minisweharness.baseline import load_baseline
from minisweharness.clocksync import ClockProbe
from minisweharness.config import SharedMountConfig, VMConfig
from minisweharness.host import host_info
from minisweharness.images import (
    DEFAULT_REGISTRY,
    HarborAPI,
    HarborClient,
    ImageMap,
    ImageResolution,
    ImageSkipped,
    MappingError,
    build_override,
    resolve_image,
)
from minisweharness.manifest import (
    EpisodeManifest,
    source_environment_from_trajectory,
    source_image_from_trajectory,
)
from minisweharness.sampler import VMMSampler
from minisweharness.store import GuestStore, GuestStoreError
from minisweharness.timestamps import now
from minisweharness.vm import SingleVM
from minisweharness.join import guest_totals, host_totals, load_jsonl, reconcile
from minisweharness.quality import QualityThresholds, evaluate_quality, quality_record

logger = logging.getLogger("minisweharness.episode")

# exec_fn(command, timeout_seconds) -> CompletedProcess；缺省走 vm.guest_exec
ExecFn = Callable[[str, float], subprocess.CompletedProcess]
# vm_factory(config, vm_id, keep_runtime_dir) -> VM 句柄；缺省 SingleVM（可注入假 VM 测试）
VMFactory = Callable[[VMConfig, str, bool], "SingleVM"]


MINI_AGENT_GUEST_DIR = "/opt/mini-swe-agent"


def _install_mini_agent(vm, execute, mini_agent_dir, ssh_transfer, shared_dir, guest_share) -> None:
    """把宿主机 mini-swe-agent 源码装进 guest（``pip install``，依赖经 guest 网络解析）。

    供 guest 镜像未预装 mini-swe-agent 时使用；镜像已预装则不要传 ``mini_agent_dir``。
    ``mini_agent_dir`` 必须是含 ``minisweagent`` 包的目录（mini 仓库的 ``src/``）。
    """
    source = Path(mini_agent_dir)
    if not (source / "minisweagent" / "__init__.py").is_file():
        raise ValueError(
            f"mini_agent_dir 必须是包含 minisweagent 包的目录（mini-swe-agent 的 src/）：{source}"
        )
    if ssh_transfer:
        vm.upload_directory(source, MINI_AGENT_GUEST_DIR, timeout=300)
        guest_src = MINI_AGENT_GUEST_DIR
    else:
        destination = shared_dir / "mini-agent"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        guest_src = f"{guest_share}/mini-agent"
    result = execute(
        f"python3 -m pip install --disable-pip-version-check --no-input {shlex.quote(guest_src)}",
        600,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr)[-2000:]
        raise GuestStoreError(
            f"pip install mini-swe-agent in guest failed ({result.returncode}): {output}; "
            "guest 需有 pip 与 PyPI 网络，或将 mini-swe-agent 预装进 guest 镜像（不传 --mini-agent-dir）"
        )


def build_replay_command(
    manifest: EpisodeManifest,
    *,
    store_guest_path: str,
    output_guest_path: str,
    environment_config_guest_path: str,
) -> list[str]:
    """guest 内 mini replay run 命令（参数与 mini ``run/replay.py`` 对齐）。"""
    if manifest.mode != "replay":
        raise ValueError("H2 仅支持 replay；record 留待后续步骤")
    command = shlex.split(manifest.mini_executable) + [
        "run",
        "--store",
        store_guest_path,
        "--source",
        manifest.source_episode,
        "--output",
        output_guest_path,
        "--episode",
        manifest.resolved_episode_id,
        "--benchmark",
        manifest.benchmark,
        "--task-id",
        manifest.task_id,
        "--attempt",
        str(manifest.attempt),
        "--environment-config",
        environment_config_guest_path,
    ]
    if manifest.profile_digest:
        command.extend(["--profile-digest", manifest.profile_digest])
    command.extend(manifest.extra_replay_args)
    return command


def export_events_command(
    store_guest_path: str,
    episode_id: str,
    output_guest_path: str,
    *,
    mini_executable: str = "python3 -m minisweagent.run.replay",
) -> list[str]:
    return shlex.split(mini_executable) + [
        "export-events",
        "--store",
        store_guest_path,
        "--episode",
        episode_id,
        "--output",
        output_guest_path,
    ]


def run_episode(
    manifest: EpisodeManifest,
    vm_config: VMConfig,
    run_dir: str | Path,
    *,
    registry: str = DEFAULT_REGISTRY,
    image_map_path: str | Path | None = None,
    harbor: HarborAPI | None = None,
    registry_username: str = "",
    registry_password: str = "",
    keep_runtime_dir: bool = False,
    exec_fn: ExecFn | None = None,
    vm_factory: VMFactory | None = None,
    baseline_profile_id: str | None = None,
    thresholds: QualityThresholds | None = None,
    slot_index: int | None = None,
    mini_agent_dir: str | Path | None = None,
) -> dict:
    """跑一个 episode；返回 episode.json 记录。异常时尽力落盘 status=failed。"""
    if bool(registry_username) != bool(registry_password):
        raise ValueError("registry_username and registry_password must be provided together")
    run_dir = Path(run_dir)
    episode_id = manifest.resolved_episode_id
    episode_dir = run_dir / "episodes" / episode_id
    shared_dir = episode_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)

    if manifest.mode != "replay":
        raise NotImplementedError("H2 仅支持 replay；record 留待后续步骤")

    source_image = ""
    source_env: dict = {}
    if manifest.trajectory:
        trajectory = json.loads(Path(manifest.trajectory).read_text())
        source_image = source_image_from_trajectory(trajectory)
        source_env = source_environment_from_trajectory(trajectory)

    # ---- 起跑前：映射表覆盖校验 + 解析（缺失/零 tag 报错，多 tag 跳过）
    resolution: ImageResolution | None = None
    skipped: ImageSkipped | None = None
    if manifest.mode == "replay":
        if image_map_path is None:
            raise MappingError("replay requires --image-map (映射表覆盖校验)")
        image_map = ImageMap.from_jsonl(image_map_path)
        entry = image_map.lookup(manifest.benchmark, manifest.task_id)
        if entry is None:
            raise MappingError(
                f"no image_map entry for {manifest.benchmark}/{manifest.task_id}"
            )
        result = resolve_image(
            entry,
            registry=registry,
            harbor=harbor or HarborClient(registry),
            source_image=source_image,
        )
        if isinstance(result, ImageSkipped):
            skipped = result
        else:
            resolution = result

    if skipped is not None:
        record = {
            "episode_id": episode_id,
            "benchmark": manifest.benchmark,
            "task_id": manifest.task_id,
            "attempt": manifest.attempt,
            "mode": manifest.mode,
            "profile_digest": manifest.profile_digest or None,
            "source_episode": manifest.source_episode or None,
            "status": "skipped",
            "slot_index": slot_index,
            "image": None,
            "image_digest": None,
            "image_source": None,
            "source_image": source_image or None,
            "image_override": None,
            "skip_reason": skipped.reason,
            "image_tags": list(skipped.tags),
            "quality": {},
        }
        episode_dir.mkdir(parents=True, exist_ok=True)
        (episode_dir / "episode.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        _update_run_manifest(
            run_dir,
            episode_id,
            "skipped",
            image_map={skipped.docker_image: {"skipped": skipped.reason, "tags": list(skipped.tags)}},
            slot_index=slot_index,
        )
        return record

    assert resolution is not None  # replay 必解析；record 形态后续步骤再接入
    override = build_override(resolution.image, source_environment=source_env)

    anchors = AnchorRecorder(episode_dir / "anchors.jsonl", episode_id)
    lifecycle = None
    sampler = None
    probe = None
    vm = None
    status = "failed"
    failure = ""
    quality = quality_record([])
    baseline_id = baseline_profile_id
    try:
        config = _episode_vm_config(vm_config, shared_dir)
        vm = (vm_factory or SingleVM)(config, episode_id, keep_runtime_dir)
        vm.create()
        lifecycle = vm.lifecycle
        execute = exec_fn or vm.guest_exec

        sampler = VMMSampler(
            vm.process.pid,
            episode_dir / "vm_samples.jsonl",
            tap_device=config.tap_device,
            extra_pids=[vm.virtiofsd_process.pid] if vm.virtiofsd_process else [],
        )
        sampler.start()

        guest_share = config.shared_mount.guest_path
        ssh_transfer = config.shared_mount.backend == "ssh"
        store_guest = f"{guest_share}/store"
        output_guest = f"{guest_share}/trajectory.json"
        events_guest = f"{guest_share}/guest_events.jsonl"
        override_guest = f"{guest_share}/override.json"

        # ---- 阶段 1：镜像预热（setup，不进 episode 测量窗口）
        anchors.mark("warmup_start")
        registry_logged_in = False
        try:
            if registry_username:
                vm.docker_login(registry, registry_username, registry_password)
                registry_logged_in = True
            _warmup_image(execute, resolution.image)
        finally:
            if registry_logged_in:
                try:
                    vm.docker_logout(registry)
                except Exception as exc:  # noqa: BLE001 — logout 失败不覆盖 warmup 结果
                    logger.warning("docker logout failed for %s: %s", registry, exc)
        anchors.mark("warmup_done")

        # ---- 阶段 2：注入（源 store + override 进共享挂载）
        if manifest.source_store:
            if ssh_transfer:
                vm.upload_directory(manifest.source_store, store_guest, timeout=300)
            else:
                destination = shared_dir / "store"
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(manifest.source_store, destination)
        (shared_dir / "override.json").write_text(json.dumps(override, indent=2) + "\n")
        if ssh_transfer:
            vm.upload_file(shared_dir / "override.json", override_guest)

        # ---- 阶段 2b：mini-swe-agent 代码供给（guest 镜像未预装时）
        if mini_agent_dir:
            _install_mini_agent(vm, execute, mini_agent_dir, ssh_transfer, shared_dir, guest_share)

        # ---- 阶段 3：episode 测量窗口（replay）
        heartbeat_guest = f"{store_guest}/heartbeat.json"
        probe = ClockProbe(
            heartbeat_guest if ssh_transfer else shared_dir / "store" / "heartbeat.json",
            read_text_fn=(lambda: vm.read_guest_file(heartbeat_guest, timeout=2)) if ssh_transfer else None,
        )
        probe.start()
        anchors.mark("episode_start")
        command = build_replay_command(
            manifest,
            store_guest_path=store_guest,
            output_guest_path=output_guest,
            environment_config_guest_path=override_guest,
        )
        logger.info("guest replay: %s", shlex.join(command))
        result = execute(shlex.join(command), manifest.episode_timeout_seconds)
        anchors.mark("episode_end")
        if result.returncode != 0:
            failure = f"replay exited {result.returncode}: {(result.stdout + result.stderr)[-2000:]}"
            raise GuestStoreError(failure)

        # ---- 阶段 4：导出 + 完整性校验
        execute(
            shlex.join(
                export_events_command(
                    store_guest,
                    episode_id,
                    events_guest,
                    mini_executable=manifest.mini_executable,
                )
            ),
            120,
        )
        if ssh_transfer:
            vm.download_directory(store_guest, shared_dir / "store", timeout=300)
            vm.download_file(events_guest, shared_dir / "guest_events.jsonl")
        store = GuestStore(shared_dir / "store")
        store.exported_count(episode_id, shared_dir / "guest_events.jsonl")
        if not store.completed(episode_id):
            raise GuestStoreError("session_snapshot.final missing; episode incomplete")

        # ---- 阶段 5：时钟校准 + 双层 join 对账
        _write_clock_sync(episode_dir, probe)
        probe = None  # 已停止，finally 不再处理
        reconciliation = _write_reconciliation(episode_dir)
        baseline = load_baseline(run_dir, baseline_profile_id)
        baseline_id = baseline.get("profile_id") if baseline else baseline_profile_id
        gaps = _coverage_gap_count(episode_dir / "vm_samples.jsonl")
        quality = quality_record(
            evaluate_quality(reconciliation, thresholds=thresholds, coverage_gaps=gaps),
            baseline_profile_id=baseline_id,
            thresholds=thresholds,
        )
        status = "completed"
    except Exception as exc:  # noqa: BLE001 — 失败也要落盘 lifecycle/episode.json
        failure = failure or f"{type(exc).__name__}: {exc}"
        logger.exception("episode %s failed", episode_id)
        try:
            if "episode_start" in anchors._phases and "episode_end" not in anchors._phases:
                anchors.mark("episode_end")  # 异常也闭合窗口，保证切窗可用
        except Exception:  # noqa: BLE001, S110
            pass
    finally:
        if probe is not None:
            probe.stop()
        if sampler is not None:
            sampler.stop()
        if vm is not None:
            vm.destroy()
        if lifecycle is not None:
            lifecycle.write(episode_dir / "vm_lifecycle.json")

    episode_json = _write_episode_json(
        manifest,
        episode_dir,
        status=status,
        failure=failure,
        resolution=resolution,
        override=override,
        source_image=source_image,
        quality=quality,
        baseline_profile_id=baseline_id,
        slot_index=slot_index,
        vm_tap_device=config.tap_device,
        vm_guest_ip=config.guest_ip,
        vm_guest_mac=config.guest_mac,
    )
    _update_run_manifest(
        run_dir,
        episode_id,
        status,
        image_map={resolution.docker_image: {"tag": resolution.tag, "digest": resolution.digest or None}},
        slot_index=slot_index,
    )
    return episode_json


def record_episode_outcome(
    manifest: EpisodeManifest,
    run_dir: str | Path,
    *,
    status: str,
    reason: str | None = None,
    slot_index: int | None = None,
) -> dict:
    """为未进入 run_episode 的 episode（如 guardrail 拒绝/调度异常）落盘结果。"""
    run_dir = Path(run_dir)
    episode_id = manifest.resolved_episode_id
    episode_dir = run_dir / "episodes" / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "episode_id": episode_id,
        "benchmark": manifest.benchmark,
        "task_id": manifest.task_id,
        "attempt": manifest.attempt,
        "mode": manifest.mode,
        "profile_digest": manifest.profile_digest or None,
        "source_episode": manifest.source_episode or None,
        "status": status,
        "slot_index": slot_index,
        "image": None,
        "image_digest": None,
        "image_source": None,
        "source_image": None,
        "image_override": None,
        "baseline_profile_id": None,
        "quality": {},
    }
    if reason:
        record["reject_reason" if status == "rejected" else "failure"] = reason
    (episode_dir / "episode.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    _update_run_manifest(run_dir, episode_id, status)
    return record


def _episode_vm_config(vm_config: VMConfig, shared_dir: Path) -> VMConfig:
    shared = vm_config.shared_mount
    mount = SharedMountConfig(
        host_dir=str(shared_dir),
        guest_path=shared.guest_path if shared else "/mnt/share",
        backend=shared.backend if shared else "ssh",
        tag=shared.tag if shared else "share",
        virtiofsd_executable=shared.virtiofsd_executable if shared else "virtiofsd",
        virtiofsd_args=list(shared.virtiofsd_args) if shared else ["-o", "cache=auto"],
    )
    return VMConfig.from_dict({**vm_config.__dict__, "shared_mount": mount.__dict__})


def _warmup_image(execute: ExecFn, image: str) -> None:
    """guest 内备好 Harbor 全名镜像（同名无 tag，直接 pull，不做预烘焙）。"""
    logger.info("warm-up pull %s", image)
    result = execute(f"docker pull {shlex.quote(image)}", 1800)
    if result.returncode != 0:
        raise GuestStoreError(f"image warm-up failed: docker pull {image}: {(result.stdout + result.stderr)[-2000:]}")
    verify = execute(f"docker image inspect {shlex.quote(image)} >/dev/null", 60)
    if verify.returncode != 0:
        raise GuestStoreError(f"image warm-up verify failed: {image}")


def _write_clock_sync(episode_dir: Path, probe: ClockProbe) -> dict:
    """停表校准，落 §6 ``clock_sync.json``（``offset_seconds/drift_ppm/probe_count``）。"""
    probe.stop()
    sync = probe.calibrate()
    (episode_dir / "clock_sync.json").write_text(json.dumps(sync, indent=2, ensure_ascii=False) + "\n")
    return sync


def _write_reconciliation(episode_dir: Path) -> dict:
    """guest 事件映射到 host 时间线后对账，落 §6 ``reconciliation.json``。"""
    sync = json.loads((episode_dir / "clock_sync.json").read_text())
    anchors = load_jsonl(episode_dir / "anchors.jsonl")
    start = next((a for a in anchors if a["phase"] == "episode_start"), None)
    end = next((a for a in anchors if a["phase"] == "episode_end"), None)
    offset = sync.get("offset_seconds")
    if offset is None or start is None or end is None:
        record = {
            "cpu_ratio": None,
            "mem_ratio": None,
            "io_ratio": None,
            "net_ratio": None,
            "notes": ["clock offset or episode anchors unavailable; reconciliation skipped"],
        }
    else:
        guest = guest_totals(
            load_jsonl(episode_dir / "shared" / "guest_events.jsonl"),
            offset_seconds=offset,
            window_start_wall=start["wall_ts"],
            window_end_wall=end["wall_ts"],
        )
        host = host_totals(
            load_jsonl(episode_dir / "vm_samples.jsonl"),
            window_start_monotonic=start["monotonic_ts"],
            window_end_monotonic=end["monotonic_ts"],
        )
        record = reconcile(guest, host)
    (episode_dir / "reconciliation.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    )
    return record


def _coverage_gap_count(vm_samples_path: Path) -> int:
    return len(coverage_gaps(load_jsonl(vm_samples_path)))


def _write_episode_json(
    manifest: EpisodeManifest,
    episode_dir: Path,
    *,
    status: str,
    failure: str,
    resolution: ImageResolution,
    override: dict,
    source_image: str,
    quality: dict,
    baseline_profile_id: str | None,
    slot_index: int | None = None,
    vm_tap_device: str = "",
    vm_guest_ip: str = "",
    vm_guest_mac: str = "",
) -> dict:
    record = {
        "episode_id": manifest.resolved_episode_id,
        "benchmark": manifest.benchmark,
        "task_id": manifest.task_id,
        "attempt": manifest.attempt,
        "mode": manifest.mode,
        "profile_digest": manifest.profile_digest or None,
        "source_episode": manifest.source_episode or None,
        "status": status,
        "slot_index": slot_index,
        "vm_tap_device": vm_tap_device or None,
        "vm_guest_ip": vm_guest_ip or None,
        "vm_guest_mac": vm_guest_mac or None,
        "image": resolution.image,
        "image_digest": resolution.digest or None,
        "image_source": resolution.image_source,
        "source_image": source_image or None,
        "image_override": override,
        "baseline_profile_id": baseline_profile_id,
        "quality": quality,
    }
    if failure:
        record["failure"] = failure
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "episode.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return record


def _update_run_manifest(
    run_dir: Path,
    episode_id: str,
    status: str,
    *,
    image_map: dict | None = None,
    slot_index: int | None = None,
) -> None:
    """§6 run_manifest.json：增量登记 episode 与 image_map（baseline/concurrency 留待 H4/H5）。"""
    path = run_dir / "run_manifest.json"
    if path.is_file():
        manifest = json.loads(path.read_text())
    else:
        manifest = {}
    defaults = {
        "run_id": run_dir.name,
        "started_wall": now().wall,
        "host": host_info(),
        "vmm": "firecracker",
        "concurrency": 1,
        "baseline_profile_id": None,
        "image_map": {},
        "episodes": [],
    }
    for key, value in defaults.items():
        manifest.setdefault(key, value)
    if image_map:
        manifest["image_map"] = {**manifest.get("image_map", {}), **image_map}
    manifest["episodes"] = [
        *[e for e in manifest.get("episodes", []) if e["episode_id"] != episode_id],
        {"episode_id": episode_id, "status": status, "slot_index": slot_index},
    ]
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
