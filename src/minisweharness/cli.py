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
from minisweharness.config import VMConfig
from minisweharness.episode import run_episode
from minisweharness.guardrail import GuardrailPolicy
from minisweharness.images import DEFAULT_REGISTRY
from minisweharness.images import HarborClient
from minisweharness.manifest import EpisodeManifest
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


def _cmd_run(args: argparse.Namespace) -> int:
    episodes = [EpisodeManifest.from_json(path) for path in args.manifest]
    policy = GuardrailPolicy(
        max_concurrency=args.concurrency,
        host_available_memory_floor_gib=args.memory_floor,
        host_disk_free_floor_gib=args.disk_floor,
        queue_on_pressure=not args.no_queue,
        admission_poll_seconds=args.poll,
        admission_timeout_seconds=args.timeout,
    )
    username, password = _registry_credentials_from_environment()
    results = run_episodes(
        episodes,
        VMConfig.from_json(args.config),
        args.run_dir,
        policy=policy,
        registry=args.registry,
        image_map_path=args.image_map,
        harbor=_harbor_from_environment(args.registry),
        registry_username=username,
        registry_password=password,
        keep_runtime_dir=args.keep_runtime_dir,
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
    run.add_argument("--config", required=True, help="VM 配置 JSON")
    run.add_argument("--run-dir", required=True, help="run 输出目录（§6 布局）")
    run.add_argument("--image-map", required=True, help="镜像映射表 JSONL（§5.4）")
    run.add_argument("--manifest", action="append", required=True, help="episode manifest JSON（可多次）")
    run.add_argument("--registry", default=DEFAULT_REGISTRY)
    run.add_argument("--concurrency", type=int, default=1, help="同时运行的 VM 数上限")
    run.add_argument("--memory-floor", type=float, default=12.0, help="宿主机可用内存下限 GiB")
    run.add_argument("--disk-floor", type=float, default=50.0, help="宿主机可用磁盘下限 GiB")
    run.add_argument("--no-queue", action="store_true", help="触下限时直接拒绝而非排队等待")
    run.add_argument("--poll", type=float, default=1.0, help="guardrail 轮询周期秒")
    run.add_argument("--timeout", type=float, default=600.0, help="排队最长等待秒")
    run.add_argument("--keep-runtime-dir", action="store_true")
    run.set_defaults(func=_cmd_run)
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
