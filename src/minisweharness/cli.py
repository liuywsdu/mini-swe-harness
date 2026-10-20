"""``mini-swe-harness`` 命令行入口。

当前子命令：

- ``smoke``：H0 单 VM 冒烟（创建 → 共享挂载 → guest hello-world → 销毁），
  在 ``--run-dir`` 下产出 ``vm_lifecycle.json`` + ``smoke.json``。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from minisweharness.baseline import measure_baseline
from minisweharness.config import VMConfig, derive_slot_config
from minisweharness.episode import run_episode
from minisweharness.guardrail import GuardrailPolicy
from minisweharness.images import DEFAULT_REGISTRY
from minisweharness.images import HarborClient
from minisweharness.manifest import EpisodeManifest
from minisweharness.prepare import prepare
from minisweharness.scheduler import run_episodes
from minisweharness.sampler import DEFAULT_INTERVAL_SECONDS, VMMSampler
from minisweharness.vm import run_h0_smoke


def _harbor_from_environment(registry: str) -> HarborClient:
    """从环境变量读取 registry 认证；密码不出现在 CLI 参数或进程列表。"""
    return HarborClient(
        registry,
        ca_cert=os.environ.get("HARBOR_CA_CERT", ""),
        token=os.environ.get("HARBOR_TOKEN", ""),
        username=os.environ.get("HARBOR_USERNAME", ""),
        password=os.environ.get("HARBOR_PASSWORD", ""),
    )


def _registry_credentials_from_environment() -> tuple[str, str]:
    username = os.environ.get("HARBOR_USERNAME", "")
    password = os.environ.get("HARBOR_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("HARBOR_USERNAME and HARBOR_PASSWORD must be provided together")
    return username, password


def _cmd_sample(args: argparse.Namespace) -> int:
    """对已有 VMM 进程树采样（H1）：--duration 缺省则跑到 Ctrl+C。"""
    sampler = VMMSampler(
        args.pid,
        args.output,
        interval_seconds=args.interval,
        tap_device=args.tap,
        extra_pids=args.extra_pid,
    )
    sampler.start()
    print(f"sampling pid tree rooted at {args.pid} -> {args.output}", file=sys.stderr)
    try:
        if args.duration is not None:
            import time

            time.sleep(args.duration)
        else:
            import time

            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
    if sampler.errors:
        print(f"warnings: {len(sampler.errors)} read errors (see null fields)", file=sys.stderr)
    return 0


def _cmd_run_episode(args: argparse.Namespace) -> int:
    manifest = EpisodeManifest.from_json(args.manifest)
    vm_config = VMConfig.from_json(args.config)
    username, password = _registry_credentials_from_environment()
    record = run_episode(
        manifest,
        vm_config,
        args.run_dir,
        registry=args.registry,
        image_map_path=args.image_map,
        harbor=_harbor_from_environment(args.registry),
        registry_username=username,
        registry_password=password,
        keep_runtime_dir=args.keep_runtime_dir,
        mini_agent_dir=args.mini_agent_dir or None,
    )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0 if record["status"] == "completed" else 1


def _cmd_baseline(args: argparse.Namespace) -> int:
    record = measure_baseline(
        VMConfig.from_json(args.config),
        args.run_dir,
        duration_seconds=args.duration,
        interval_seconds=args.interval,
        profile_id=args.profile_id or None,
        keep_runtime_dir=args.keep_runtime_dir,
    )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def _manifest_paths(args: argparse.Namespace) -> list[str]:
    """--manifest 可多次且每次可空格分隔多个；--manifest-dir 追加目录内全部 *.json。"""
    paths = [path for group in (args.manifest or []) for path in group]
    if args.manifest_dir:
        paths.extend(sorted(str(p) for p in Path(args.manifest_dir).glob("*.json")))
    return paths


def _cmd_run(args: argparse.Namespace) -> int:
    manifest_paths = _manifest_paths(args)
    if not manifest_paths:
        raise ValueError("no manifests: pass --manifest <file...> and/or --manifest-dir <dir>")
    episodes = [EpisodeManifest.from_json(path) for path in manifest_paths]
    policy = GuardrailPolicy(
        max_concurrency=args.concurrency,
        host_available_memory_floor_gib=args.memory_floor,
        host_disk_free_floor_gib=args.disk_floor,
        queue_on_pressure=not args.no_queue,
        admission_poll_seconds=args.poll,
        admission_timeout_seconds=args.timeout,
    )
    username, password = _registry_credentials_from_environment()
    configs = [VMConfig.from_json(path) for path in args.config]
    if len(configs) == 1 and args.concurrency > 1:
        # 单 config 自动按槽位派生网络三元组（tap/guest_ip/guest_mac 互不冲突）
        configs = [
            derive_slot_config(configs[0], slot, ip_mode=args.ip_mode)
            for slot in range(args.concurrency)
        ]
        for slot, config in enumerate(configs):
            print(
                f"slot {slot}: tap={config.tap_device} guest_ip={config.guest_ip} mac={config.guest_mac}",
                file=sys.stderr,
            )
        print(
            "hint: prepare host networking first, e.g. "
            "`mini-swe-harness make-configs --base <config> --slots %d --ip-mode %s` prints the commands"
            % (args.concurrency, args.ip_mode),
            file=sys.stderr,
        )
    results = run_episodes(
        episodes,
        configs,
        args.run_dir,
        policy=policy,
        registry=args.registry,
        image_map_path=args.image_map,
        harbor=_harbor_from_environment(args.registry),
        registry_username=username,
        registry_password=password,
        keep_runtime_dir=args.keep_runtime_dir,
        mini_agent_dir=args.mini_agent_dir or None,
    )
    for record in results:
        print(json.dumps(record, ensure_ascii=False))
    failed = [r for r in results if r.get("status") not in ("completed", "skipped")]
    print(f"{len(results)} episodes, {len(failed)} not completed", file=sys.stderr)
    return 1 if failed else 0


def _cmd_smoke(args: argparse.Namespace) -> int:
    config = VMConfig.from_json(args.config)
    record = run_h0_smoke(config, Path(args.run_dir), keep_runtime_dir=args.keep_runtime_dir)
    print(json.dumps(record, indent=2, ensure_ascii=False))
    print(f"vm_lifecycle.json written to {Path(args.run_dir) / 'vm_lifecycle.json'}", file=sys.stderr)
    return 0


def _cmd_make_configs(args: argparse.Namespace) -> int:
    """按并发槽位展开 VM 配置并打印宿主机网络准备命令。"""
    base = VMConfig.from_json(args.base)
    ipaddress = __import__("ipaddress")
    base_ip = int(ipaddress.IPv4Address(base.guest_ip))
    configs = []
    for slot in range(args.slots):
        config = derive_slot_config(base, slot, ip_mode=args.ip_mode)
        configs.append(config)
        path = Path(args.out_dir) / f"vm_config.slot{slot}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(config.__dict__)
        if data["shared_mount"] is not None:
            data["shared_mount"] = dict(data["shared_mount"].__dict__)
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"# slot {slot}: {path}", file=sys.stderr)
        print(f"#   tap={config.tap_device} guest_ip={config.guest_ip} mac={config.guest_mac}", file=sys.stderr)
    print("sudo sysctl -w net.ipv4.ip_forward=1")
    print("# 出网（guest 拉 Harbor）按需启用 MASQUERADE，<出口网卡> 替换为实际接口：")
    print("# sudo iptables -t nat -A POSTROUTING -o <出口网卡> -j MASQUERADE")
    for slot, config in enumerate(configs):
        if args.ip_mode == "third_octet":
            # 每 tap 独立 /30：172.16.<slot>.1/30 ↔ guest 172.16.<slot>.2
            host_ip = str(ipaddress.IPv4Address((base_ip & 0xFFFF0000) + slot * 256 + 1))
            print(f"sudo ip tuntap add {config.tap_device} mode tap user \"$USER\"")
            print(f"sudo ip addr add {host_ip}/30 dev {config.tap_device}")
            print(f"sudo ip link set {config.tap_device} up")
        else:
            # 所有 tap 挂同一 bridge，bridge 占 base 所在 /24 的 .1
            bridge_ip = str(ipaddress.IPv4Address(base_ip & 0xFFFFFF00 | 1))
            if slot == 0:
                print(f"sudo ip link add br0 type bridge")
                print(f"sudo ip addr add {bridge_ip}/24 dev br0")
                print("sudo ip link set br0 up")
            print(f"sudo ip tuntap add {config.tap_device} mode tap user \"$USER\"")
            print(f"sudo ip link set {config.tap_device} master br0")
            print(f"sudo ip link set {config.tap_device} up")
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    """traces 目录批量准备：import 进 source store + 生成 episode manifest + 覆盖校验。"""
    report = prepare(
        args.traces_dir,
        args.image_map,
        args.store_dir,
        args.manifest_dir,
        attempts=args.attempts,
        profile_digest=args.profile_digest,
        skip_import=args.skip_import,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["missing_image"]:
        print(
            f"warning: {len(report['missing_image'])} task(s) missing from image map; "
            "manifests were not generated for them",
            file=sys.stderr,
        )
    if report["import_failed"]:
        print(f"error: {len(report['import_failed'])} episode(s) failed to import", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-swe-harness", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug 日志")
    subcommands = parser.add_subparsers(dest="command", required=True)

    smoke = subcommands.add_parser("smoke", help="H0 单 VM 冒烟（生命周期时间戳齐全为验收）")
    smoke.add_argument("--config", required=True, help="JSON 配置文件（见 examples/h0_smoke.json）")
    smoke.add_argument("--run-dir", required=True, help="本次运行输出目录")
    smoke.add_argument("--keep-runtime-dir", action="store_true", help="保留 firecracker 运行时目录便于排障")
    smoke.set_defaults(func=_cmd_smoke)

    sample = subcommands.add_parser("sample", help="H1 VMM 进程树采样 → vm_samples.jsonl")
    sample.add_argument("--pid", type=int, required=True, help="VMM 根进程 pid（firecracker/jailer）")
    sample.add_argument("--output", required=True, help="JSONL 输出路径")
    sample.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="采样周期秒（默认 0.5）")
    sample.add_argument("--tap", default=None, help="tap 设备名（读 net/dev 计数）")
    sample.add_argument("--extra-pid", type=int, action="append", default=[], help="额外纳入的进程（如 virtiofsd）")
    sample.add_argument("--duration", type=float, default=None, help="采样时长秒；缺省跑到 Ctrl+C")
    sample.set_defaults(func=_cmd_sample)

    episode = subcommands.add_parser("run-episode", help="H2 episode 注入 + 镜像预热 + mini replay")
    episode.add_argument("--manifest", required=True, help="episode manifest JSON")
    episode.add_argument("--config", required=True, help="VM 配置 JSON（shared_mount 由 harness 按 episode 覆写）")
    episode.add_argument("--run-dir", required=True, help="run 输出目录（§6 布局）")
    episode.add_argument("--registry", default=DEFAULT_REGISTRY, help="Harbor registry 前缀")
    episode.add_argument("--image-map", required=True, help="镜像映射表 JSONL（§5.4）")
    episode.add_argument("--keep-runtime-dir", action="store_true")
    episode.add_argument(
        "--mini-agent-dir",
        default="",
        help="宿主机 mini-swe-agent 仓库根（或其 src/ 子目录，自动识别）；guest 未预装时上传并 pip install",
    )
    episode.set_defaults(func=_cmd_run_episode)

    baseline = subcommands.add_parser("baseline", help="H4 空 VM 空转基线 → baseline_<profile_id>.json")
    baseline.add_argument("--config", required=True, help="VM 配置 JSON（与 episode 同构）")
    baseline.add_argument("--run-dir", required=True, help="run 输出目录（§6 布局）")
    baseline.add_argument("--duration", type=float, default=30.0, help="空转采样时长秒（默认 30）")
    baseline.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="采样周期秒（默认 0.5）")
    baseline.add_argument("--profile-id", default="", help="显式 profile id；缺省自动生成")
    baseline.add_argument("--keep-runtime-dir", action="store_true")
    baseline.set_defaults(func=_cmd_baseline)

    run = subcommands.add_parser("run", help="H5 并行调度 episode 矩阵 + guardrail")
    run.add_argument(
        "--config",
        action="append",
        required=True,
        help="VM 配置 JSON（可多次，每并发槽位一份；仅一份时按 --concurrency/--ip-mode 自动派生）",
    )
    run.add_argument(
        "--ip-mode",
        choices=["last_octet", "third_octet"],
        default="last_octet",
        help="单 config 自动派生 guest_ip 的方式：末字节（bridge+/24，默认）或第三字节（每 tap /30）",
    )
    run.add_argument("--run-dir", required=True, help="run 输出目录（§6 布局）")
    run.add_argument("--image-map", required=True, help="镜像映射表 JSONL（§5.4）")
    run.add_argument(
        "--manifest",
        action="append",
        nargs="+",
        help="episode manifest JSON（可多次；每次可空格分隔多个文件）",
    )
    run.add_argument("--manifest-dir", default="", help="manifest 目录：其中全部 *.json 加入 episode 矩阵")
    run.add_argument("--registry", default=DEFAULT_REGISTRY)
    run.add_argument("--concurrency", type=int, default=1, help="同时运行的 VM 数上限")
    run.add_argument("--memory-floor", type=float, default=12.0, help="宿主机可用内存下限 GiB")
    run.add_argument("--disk-floor", type=float, default=50.0, help="宿主机可用磁盘下限 GiB")
    run.add_argument("--no-queue", action="store_true", help="触下限时直接拒绝而非排队等待")
    run.add_argument("--poll", type=float, default=1.0, help="guardrail 轮询周期秒")
    run.add_argument("--timeout", type=float, default=600.0, help="排队最长等待秒")
    run.add_argument("--keep-runtime-dir", action="store_true")
    run.add_argument(
        "--mini-agent-dir",
        default="",
        help="宿主机 mini-swe-agent 仓库根（或其 src/ 子目录，自动识别）；guest 未预装时上传并 pip install",
    )
    run.set_defaults(func=_cmd_run)

    make_configs = subcommands.add_parser(
        "make-configs", help="按并发槽位展开 VM 配置（网络三元组互不冲突）并打印宿主机 tap/bridge 命令"
    )
    make_configs.add_argument("--base", required=True, help="base VM 配置 JSON（视为 slot 0）")
    make_configs.add_argument("--slots", type=int, required=True, help="并发槽位数")
    make_configs.add_argument("--ip-mode", choices=["last_octet", "third_octet"], default="last_octet")
    make_configs.add_argument("--out-dir", required=True, help="槽位配置输出目录")
    make_configs.set_defaults(func=_cmd_make_configs)

    prep = subcommands.add_parser(
        "prepare", help="traces 目录批量准备：import 进 source store + 生成 episode manifest + 映射表覆盖校验"
    )
    prep.add_argument("--traces-dir", required=True, help="traces 根目录（<benchmark>/<task_id>/trajectory.json）")
    prep.add_argument("--image-map", required=True, help="镜像映射表 JSONL（覆盖校验；tasks.jsonl 同构）")
    prep.add_argument("--store-dir", required=True, help="source store 输出目录（每源 episode 一份：<benchmark>/<source_episode_id>/）")
    prep.add_argument("--manifest-dir", required=True, help="episode manifest 输出目录")
    prep.add_argument("--attempts", type=int, default=1, help="每个 task 生成的 attempt 份数（默认 1）")
    prep.add_argument("--profile-digest", default="", help="写入源 episode metadata 与 manifest 的测量 profile digest")
    prep.add_argument("--skip-import", action="store_true", help="只做覆盖校验/manifest 生成，不写 store")
    prep.add_argument("--dry-run", action="store_true", help="只出报告，不写 store 与 manifest")
    prep.set_defaults(func=_cmd_prepare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 — CLI 统一报错出口
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
