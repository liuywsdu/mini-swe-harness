"""空 VM 基线测量（§5.1 / §5.3 H4 / §6）。

让一台「什么都不干」的 VM 空转采样，产出 ``baseline_<profile_id>.json`` 统计
摘要（与 vm_samples 同构的指标），并把 ``baseline_profile_id`` 写回
run_manifest。基线用于把 episode 的 VMM 开销与「VM 固定开销（VMM 模拟 /
guest 内核 / virtiofsd / SSH 等）」分离，也是 reconciliation 阈值判定的参照。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable

from minisweharness.analysis import coverage_gaps, memory_time
from minisweharness.config import VMConfig
from minisweharness.host import host_info
from minisweharness.sampler import VMMSampler
from minisweharness.timestamps import now
from minisweharness.vm import SingleVM

# sampler_factory(pid, output_path, interval_seconds) -> 带 start()/stop() 的采样器
SamplerFactory = Callable[[int, Path, float], object]
# vm_factory(config, vm_id, keep_runtime_dir) -> 带 create()/destroy()/process/lifecycle 的 VM
VMFactory = Callable[[VMConfig, str, bool], SingleVM]


def compute_summary(vm_samples: list[dict], *, interval_seconds: float) -> dict:
    """把 vm_samples 汇总成基线统计（速率/峰值/积分/coverage）。"""
    if not vm_samples:
        return {}
    first, last = vm_samples[0], vm_samples[-1]
    duration = last["monotonic_ts"] - first["monotonic_ts"] or 1e-9

    def rate(field: str) -> float | None:
        before, after = first.get(field), last.get(field)
        return (after - before) / duration if before is not None and after is not None else None

    rss = [sample.get("rss_bytes") for sample in vm_samples if sample.get("rss_bytes") is not None]
    pss = [sample.get("pss_bytes") for sample in vm_samples if sample.get("pss_bytes") is not None]
    return {
        "duration_seconds": duration,
        "sample_count": len(vm_samples),
        "cpu_usec_per_sec": rate("cpu_usec"),
        "io_read_bytes_per_sec": rate("io_read_bytes"),
        "io_write_bytes_per_sec": rate("io_write_bytes"),
        "net_rx_bytes_per_sec": rate("net_rx_bytes"),
        "net_tx_bytes_per_sec": rate("net_tx_bytes"),
        "rss_peak_bytes": max(rss, default=None),
        "rss_mean_bytes": (sum(rss) / len(rss)) if rss else None,
        "pss_peak_bytes": max(pss, default=None),
        "memory_time_rss_bytes_s": memory_time(vm_samples, "rss_bytes"),
        "coverage_gaps": len(coverage_gaps(vm_samples)),
        "interval_seconds": interval_seconds,
    }


def measure_baseline(
    vm_config: VMConfig,
    run_dir: str | Path,
    *,
    duration_seconds: float = 30.0,
    interval_seconds: float = 0.5,
    profile_id: str | None = None,
    vm_factory: VMFactory | None = None,
    sampler_factory: SamplerFactory | None = None,
    keep_runtime_dir: bool = False,
) -> dict:
    """空 VM 空转采样 → 写 ``baseline_<profile_id>.json`` + 回写 run_manifest。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    profile_id = profile_id or uuid.uuid4().hex[:12]
    started_wall = now().wall
    vm_id = f"baseline-{profile_id}"
    vm = (vm_factory or SingleVM)(vm_config, vm_id, keep_runtime_dir)
    output_path = run_dir / f"baseline_{profile_id}.vm_samples.jsonl"
    sampler = None
    try:
        vm.create()
        sampler = (sampler_factory or _default_sampler_factory)(vm.process.pid, output_path, interval_seconds)
        sampler.start()
        time.sleep(duration_seconds)
    finally:
        if sampler is not None:
            sampler.stop()
        if vm is not None:
            vm.destroy()

    summary = compute_summary(_load_samples(output_path), interval_seconds=interval_seconds)
    record = {
        "profile_id": profile_id,
        "vmm": "firecracker",
        "started_wall": started_wall,
        "requested_duration_seconds": duration_seconds,
        **summary,
    }
    (run_dir / f"baseline_{profile_id}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    )
    _set_baseline_profile(run_dir, profile_id)
    return record


def load_baseline(run_dir: str | Path, profile_id: str | None = None) -> dict | None:
    """读基线统计；未给 profile_id 时从 run_manifest 取。"""
    run_dir = Path(run_dir)
    if profile_id is None:
        manifest_path = run_dir / "run_manifest.json"
        if manifest_path.is_file():
            profile_id = json.loads(manifest_path.read_text()).get("baseline_profile_id")
    if not profile_id:
        return None
    path = run_dir / f"baseline_{profile_id}.json"
    return json.loads(path.read_text()) if path.is_file() else None


def _load_samples(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _default_sampler_factory(pid: int, output_path: Path, interval_seconds: float) -> VMMSampler:
    return VMMSampler(pid, output_path, interval_seconds=interval_seconds)


def _set_baseline_profile(run_dir: Path, profile_id: str) -> None:
    path = run_dir / "run_manifest.json"
    if path.is_file():
        manifest = json.loads(path.read_text())
    else:
        manifest = {
            "run_id": run_dir.name,
            "started_wall": now().wall,
            "host": host_info(),
            "vmm": "firecracker",
            "concurrency": 1,
            "baseline_profile_id": None,
            "image_map": {},
            "episodes": [],
        }
    manifest["baseline_profile_id"] = profile_id
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
