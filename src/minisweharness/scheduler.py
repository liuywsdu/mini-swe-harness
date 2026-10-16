"""VM 池并行调度 + guardrail（§5.1 / §5.3 H5）。

按 ``GuardrailPolicy.max_concurrency`` 限制同时运行的 VM 数；每次接入新 episode
前读宿主机快照，可用内存/磁盘低于下限时按策略排队等待或拒绝，绝不静默超载。
事件隔离由 ``run_episode`` 的独立 episode 目录保证。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from minisweharness.config import VMConfig
from minisweharness.episode import record_episode_outcome, run_episode
from minisweharness.guardrail import GuardrailPolicy, HostSnapshot, admit, read_host_snapshot
from minisweharness.manifest import EpisodeManifest

# run_episode_fn(manifest) -> record
EpisodeRunner = Callable[[EpisodeManifest], dict]
# snapshot_fn() -> HostSnapshot
SnapshotFn = Callable[[], HostSnapshot]


def run_episodes(
    episodes: list[EpisodeManifest],
    vm_config: VMConfig,
    run_dir: str | Path,
    *,
    policy: GuardrailPolicy | None = None,
    snapshot_fn: SnapshotFn | None = None,
    run_episode_fn: EpisodeRunner | None = None,
    **run_kwargs,
) -> list[dict]:
    """并行跑 episode 矩阵；返回逐 episode 结果（含 rejected）。"""
    run_dir = Path(run_dir)
    policy = policy or GuardrailPolicy()
    snapshot_fn = snapshot_fn or (lambda: read_host_snapshot(run_dir))
    run_fn = run_episode_fn or (
        lambda manifest: run_episode(manifest, vm_config, run_dir, **run_kwargs)
    )
    results: list[dict] = []
    futures: list[tuple[EpisodeManifest, object]] = []
    with ThreadPoolExecutor(max_workers=policy.max_concurrency) as executor:
        for manifest in episodes:
            admitted, reason = _wait_for_admission(policy, snapshot_fn)
            if not admitted:
                results.append(record_episode_outcome(manifest, run_dir, status="rejected", reason=reason))
                continue
            futures.append((manifest, executor.submit(run_fn, manifest)))
        for manifest, future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 — 单个 episode 失败不拖垮矩阵
                results.append(
                    record_episode_outcome(
                        manifest,
                        run_dir,
                        status="failed",
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )
    return results


def _wait_for_admission(policy: GuardrailPolicy, snapshot_fn: SnapshotFn) -> tuple[bool, str]:
    deadline = time.monotonic() + policy.admission_timeout_seconds
    while True:
        admitted, reason = admit(snapshot_fn(), policy)
        if admitted:
            return True, ""
        if not policy.queue_on_pressure:
            return False, reason
        if time.monotonic() >= deadline:
            return False, f"{reason} (queue timeout)"
        time.sleep(policy.admission_poll_seconds)
