"""VM 池并行调度 + guardrail + 并发网络槽位（§5.1 / §5.3 H5）。

按 ``GuardrailPolicy.max_concurrency`` 限制同时运行的 VM 数；每次接入新 episode
前读宿主机快照，可用内存/磁盘低于下限时按策略排队等待或拒绝，绝不静默超载。
事件隔离由 ``run_episode`` 的独立 episode 目录保证。

并发网络槽位：每个并发 VM 独占一套 ``tap_device``/``guest_ip``/``guest_mac``
（一个 tap 只能挂一个 VMM）。``run_episodes`` 接受 config 池（单 config 或
列表，列表可用 ``config.derive_slot_config`` 预派生），为每个进入运行的
episode 分配最低空闲槽位，episode 记录里落 ``slot_index`` 与网络身份，保证
事后能把测量数据对回具体 tap/IP。config 池内三元组重复在起跑前报错。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Union

from minisweharness.config import VMConfig
from minisweharness.episode import record_episode_outcome, run_episode
from minisweharness.guardrail import GuardrailPolicy, HostSnapshot, admit, read_host_snapshot
from minisweharness.manifest import EpisodeManifest

# run_episode_fn(manifest, slot_index) -> record
EpisodeRunner = Callable[[EpisodeManifest, int], dict]
# snapshot_fn() -> HostSnapshot
SnapshotFn = Callable[[], HostSnapshot]


class _SlotPool:
    """最低空闲槽位分配；槽位即 config 池下标，取完则阻塞等待回收。"""

    def __init__(self, size: int):
        self._free = list(range(size))
        self._cond = threading.Condition()

    def acquire(self) -> int:
        with self._cond:
            while not self._free:
                self._cond.wait()
            return self._free.pop(0)

    def release(self, slot: int) -> None:
        with self._cond:
            self._free.append(slot)
            self._free.sort()
            self._cond.notify()


def as_config_pool(vm_configs: Union[VMConfig, list[VMConfig]]) -> list[VMConfig]:
    """单 config 视为单槽位池；列表原样使用。池为空报错。"""
    pool = [vm_configs] if isinstance(vm_configs, VMConfig) else list(vm_configs)
    if not pool:
        raise ValueError("vm config pool must not be empty")
    return pool


def validate_config_pool(pool: list[VMConfig]) -> None:
    """起跑前校验：config 池内 tap_device/guest_ip/guest_mac 必须两两不同。"""
    for field in ("tap_device", "guest_ip", "guest_mac"):
        values = [getattr(config, field) for config in pool]
        seen: dict[str, int] = {}
        for index, value in enumerate(values):
            if value in seen:
                raise ValueError(
                    f"vm config pool conflict: {field}={value!r} shared by "
                    f"slot {seen[value]} and slot {index}; derive per-slot configs "
                    "with config.derive_slot_config"
                )
            seen[value] = index


def run_episodes(
    episodes: list[EpisodeManifest],
    vm_configs: Union[VMConfig, list[VMConfig]],
    run_dir: str | Path,
    *,
    policy: GuardrailPolicy | None = None,
    snapshot_fn: SnapshotFn | None = None,
    run_episode_fn: EpisodeRunner | None = None,
    **run_kwargs,
) -> list[dict]:
    """并行跑 episode 矩阵；返回逐 episode 结果（含 rejected）。

    ``vm_configs`` 为 config 池：并发 VM 各取一个槽位配置（网络三元组互不冲突），
    默认 runner 把 ``slot_index`` 传入 ``run_episode`` 落盘。槽位数小于
    ``max_concurrency`` 时由槽池节流（多余并发在接入前阻塞等待回收）。
    """
    run_dir = Path(run_dir)
    policy = policy or GuardrailPolicy()
    pool = as_config_pool(vm_configs)
    validate_config_pool(pool)
    slots = _SlotPool(len(pool))
    snapshot_fn = snapshot_fn or (lambda: read_host_snapshot(run_dir))
    run_fn = run_episode_fn or (
        lambda manifest, slot_index: run_episode(
            manifest, pool[slot_index], run_dir, slot_index=slot_index, **run_kwargs
        )
    )
    results: list[dict] = []
    futures: list[tuple[EpisodeManifest, int, object]] = []
    with ThreadPoolExecutor(max_workers=policy.max_concurrency) as executor:
        for manifest in episodes:
            admitted, reason = _wait_for_admission(policy, snapshot_fn)
            if not admitted:
                results.append(record_episode_outcome(manifest, run_dir, status="rejected", reason=reason))
                continue
            slot_index = slots.acquire()
            future = executor.submit(run_fn, manifest, slot_index)
            # episode 完成（含异常）即回收槽位，否则提交循环会在池满时死等
            future.add_done_callback(lambda _f, s=slot_index: slots.release(s))
            futures.append((manifest, slot_index, future))
        for manifest, slot_index, future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 — 单个 episode 失败不拖垮矩阵
                results.append(
                    record_episode_outcome(
                        manifest,
                        run_dir,
                        status="failed",
                        reason=f"{type(exc).__name__}: {exc}",
                        slot_index=slot_index,
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
