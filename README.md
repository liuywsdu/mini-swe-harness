# mini-swe-harness

VM harness：管理 VM 生命周期，并记录宿主机/VM 层性能开销（计划文档
`BENCHMARK_DATA_COLLECTION_PLAN.md` 工作线 B）。mini-swe-agent 运行在 VM 内
（VM 即它的宿主机），task 在 VM 内的 docker/local 环境执行；harness 在真实
宿主机上管理 firecracker microVM，并从 VMM 进程树采样六指标。

- 纯 Python 标准库实现，无第三方依赖；
- 启动参数与镜像约定复用 mini-swe-agent `executors/firecracker.py` /
  `executors/ssh_vm.py`（guest_ip/tap/guest_mac/boot_args/SSH 约定），但代码独立。

## 目录结构

```
src/minisweharness/
  cli.py            # mini-swe-harness smoke / sample 入口
  config.py         # JSON 配置加载（VMConfig / SharedMountConfig）
  firecracker.py    # firecracker API socket 薄客户端（含 virtio-fs 设备）
  vm.py             # SingleVM 生命周期：创建/共享挂载/guest 执行/销毁 + H0 冒烟
  sampler.py        # H1 VMM 采样器：进程树 /proc 采样 → vm_samples.jsonl
  analysis.py       # 窗口差分 / memory-time 梯形积分 / coverage 缺口
  manifest.py       # H2 EpisodeManifest：episode 身份（benchmark/task_id/attempt）
  images.py         # H2 镜像映射表 + Harbor tag/digest 解析 + override 生成
  anchors.py        # H2 阶段锚点（warmup/episode 切窗，host 双时间戳）
  store.py          # H2 GuestStore：读共享挂载上的 store，校验 final/导出
  episode.py        # H2 运行器：VM + 采样 + 预热 + 注入 + replay + 导出校验
  clocksync.py      # H3 时钟探针：读 guest 心跳 → offset_seconds/drift_ppm
  join.py           # H3 双层 join：guest 映射 host 时间线 + 对账比值
  baseline.py       # H4 空 VM 基线：空转采样 → baseline_<profile_id>.json
  quality.py        # H4 质量记录：对账比值/coverage 超阈值 → quality 条目
  host.py           # 宿主机信息 + run_manifest 默认形状
  guardrail.py      # H5 guardrail：并发上限 + 内存/磁盘下限接入判定
  scheduler.py      # H5 VM 池并行调度：槽位化 config 池 + 排队/拒绝，无静默超载
  prepare.py        # 批量准备：traces → source store import + episode manifest 生成 + 映射表覆盖校验
  lifecycle.py      # VMLifecycle → vm_lifecycle.json（§6 规范）
  timestamps.py     # wall + monotonic 双时间戳（横向前提①）
tests/              # 纯逻辑单测（伪造 proc 树 + 注入时钟，无需 VM/Linux）
examples/h0_smoke.json
examples/h2_episode.json
examples/image_map.jsonl
```

## H0：单 VM 冒烟

创建 firecracker VM → 建立 guest 目录 → guest 内 hello-world（含共享目录
双向读写校验）→ 销毁，全程记录 host wall + monotonic 双时间戳。

`shared_mount.backend` 默认是 `ssh`：通过 SCP 显式上传/下载文件和目录，
不调用 Firecracker `/virtio-fs`，也不要求 virtiofsd。仅当 VMM 确实支持相应
API 时才使用 `backend: "virtiofs"`。

SCP 使用传统协议模式（`scp -O`），因此 guest 不需要启用 SFTP subsystem；
只需 sshd 与远端 `scp` 命令可用。

```bash
pip install -e .
mini-swe-harness smoke --config examples/h0_smoke.json --run-dir runs/h0-001
```

产出：

- `vm_lifecycle.json`：`{vm_id, vmm, create_start, ready, destroy_confirmed, ...}`
  （§6 规范三时间戳为 host wall，另附 `*_monotonic` 与 `phases` 子阶段时间戳）；
- `smoke.json`：hello-world 输出 + 共享挂载双向校验结果。

### H0 验收

1. `vm_lifecycle.json` 的 `create_start` / `ready` / `destroy_confirmed` 齐全
   （时间戳缺失时 `smoke` 命令非零退出）；
2. `smoke.json` 中 `shared_mount.host_to_guest_ok` 与 `guest_to_host_ok` 均为 true。

真机验收（需 Linux + /dev/kvm + tap 设备 + firecracker + ssh/scp + guest 内核与
rootfs，SSH 免密可达）：

```bash
pytest tests -q
mini-swe-harness -v smoke --config <真机配置>.json --run-dir runs/h0-accept
```

## H1：VMM 采样器

对 VMM 进程树（根 pid + 全部后代，可附加 virtiofsd 等旁路进程）按 500ms
周期采样，写 `vm_samples.jsonl`（§6 规范字段，`layer: "host_vmm"`）：

- `cpu_usec` / `io_*`：累计计数器，窗口消耗由 `analysis.window_deltas` 差分；
- `rss_bytes` / `pss_bytes`：瞬时值，memory-time 由 `analysis.memory_time` 梯形积分；
- `net_*`：tap 设备 `/proc/net/dev` 累计字节；
- `coverage_gap`：距上个样本漏采周期数（>3 由 `analysis.coverage_gaps` 标事故）；
- 不支持读（无权限/接口不存在/非 Linux）记 null（横向前提③）。

```bash
mini-swe-harness sample --pid <firecracker_pid> --output runs/ep1/vm_samples.jsonl     --interval 0.5 --tap tap0 --duration 300
```

`--pid` 可取 H0 smoke 过程中 firecracker 进程 pid；H2 起由 episode 流程自动挂接。

## H2：episode 注入 + 镜像预热

单 task 全流程：备 VM + SSH 共享目录（episode 目录下 `shared/`）→ 挂
VMM 采样器 → guest 内镜像预热 → SCP 注入源 store/override → guest 内 mini
replay → SCP 下载 store/导出事件并做完整性校验 → 按 §6 布局落盘。

```bash
mini-swe-harness run-episode \
    --manifest examples/h2_episode.json \
    --config <真机 VM 配置>.json \
    --run-dir runs/h2-001 \
    --registry harbor.internal \
    --image-map examples/image_map.jsonl \
    --mini-agent-dir /path/to/mini-swe-agent   # guest 未预装 mini-swe-agent 时（仓库根或其 src/ 均可）
```

流程要点（计划文档 §5.3 H2 / §5.4）：

- **映射表覆盖校验（起跑前）**：JSONL `{benchmark, task_id, docker_image}`
  （`docker_image` 为 `bench/task` 无 tag 相对路径；查询先精确后通配）。
  每条目经 Harbor API 列 tag —— 唯一则用，多个标
  `skipped: ambiguous_tags`，零 tag/缺条目起跑前报错。
- **digest 固定**：tag 解析后查 Harbor API 固化 digest（`:local` 等可变标签
  必须记 digest），写入 `episode.json` 与 `run_manifest.image_map`。
- **镜像预热**：warm-up 阶段 guest 内 `docker pull <Harbor 全路径>`（同名无
  tag，不做预烘焙）再 `image inspect` 验证，`image_source: harbor_direct`。
- **override 生成**：`image` 直写 Harbor 全路径，`cwd`/`timeout` 直传、
  `user: N` → `run_args: ["--user", "N"]`，`runtime: "bounded Docker"` 等未知
  字段丢弃（禁止透传 trace 环境 dict），内容落 `episode.json.image_override`。
- **注入**：SSH 后端以 `scp -r` 上传源 store、以 `scp` 上传 override，guest 内跑
  `python -m minisweagent.run.replay run --environment-config <override>`（mini 对 docker
  executor 强制 `--pull=never`，命中本地 Harbor 名镜像）。
- **mini-swe-agent 供给**：replay 在 guest 内执行，guest 必须能 import
  `minisweagent`。两种供给方式（二选一）：把它**预装进 guest 镜像**（推荐，零
  per-episode 开销）；或传 `--mini-agent-dir <mini 仓库根或其 src/>`（两种都认，
  自动识别），harness 在注入阶段把 `pyproject.toml` + `src/` 暂存上传进 guest
  并 `pip install`（依赖经 guest 网络解析，guest 需有 pip 与 PyPI 访问；失败给出
  明确报错）。
- **完整性**：`session_snapshot.final` 存在才算 episode 有效；
  guest `export-events` 后通过 SCP 下载 store 与 `guest_events.jsonl`，导出
  产物行数必须与下载后的 store 行数一致。
- **锚点切窗**：`warmup_start/warmup_done` 与 `episode_start/episode_end` 为
  host 双时间戳，pull 流量落在 warm-up 窗口，不进 episode 测量窗口。

产出（§6 布局）：`episodes/<episode_id>/` 下 `episode.json`、
`guest_events.jsonl`、`vm_samples.jsonl`、`vm_lifecycle.json`、
`anchors.jsonl`，run 根目录另写 `run_manifest.json`。

### H2 验收

1. `guest_events.jsonl` 导出完整、`session_snapshot.final` 存在（否则
   `run-episode` 非零退出且 `episode.json` 记 `status: failed`）；
2. 映射表覆盖校验生效：缺条目/零 tag 起跑前报错，多 tag 标
   `skipped: ambiguous_tags` 不进 VM；
3. `episode.json` 的 `image_source == "harbor_direct"`、`image`/`image_digest`
   齐全；replay `--pull=never` 命中本地 Harbor 名镜像；
4. pull 流量不落入 episode 测量窗口（按 `anchors.jsonl` 的
   `warmup_done → episode_start` 切窗验证）。

私有 registry 认证通过环境变量提供，不写入 manifest 或命令行：

```bash
export HARBOR_USERNAME='robot$project+h2'
read -rsp 'Harbor password: ' HARBOR_PASSWORD; export HARBOR_PASSWORD; echo
# 自签名 CA 时另设：export HARBOR_CA_CERT=/path/to/ca.crt
```

`HARBOR_USERNAME/HARBOR_PASSWORD` 同时用于 Harbor API Basic Auth 与 guest
内 `docker login --password-stdin`；镜像预热后立即 `docker logout`。也可只设
`HARBOR_TOKEN` 作为 API Bearer token，但它不能替代 guest 拉取私有镜像所需的
用户名/密码。

真机验收（需 Linux/KVM + guest 内 docker + Harbor 可达 + 已索引源 store）：

```bash
pytest tests -q
mini-swe-harness run-episode --manifest <真机 manifest>.json --config <真机 VM 配置>.json --run-dir runs/h2-accept -v
```

## H3：时钟校准 + 双层 join

episode 测量窗口内周期读 guest 心跳文件（mini `Heartbeat` 写
`store/heartbeat.json`）并打 host 双时间戳；SSH 后端通过远端 `cat` 读取，
virtiofs 后端直接读共享路径。产出 `clock_sync.json`：
`{offset_seconds, drift_ppm?, probe_count}`（offset 取中位数，drift 为 offset
对 host monotonic 的斜率 ×1e6）。随后把 `guest_events.jsonl` 的 guest wall
按「guest wall − offset → host wall」映射到 host 时间线，与
`vm_samples.jsonl` 在同一锚点窗口（`episode_start → episode_end`）内分别汇总，
产出 `reconciliation.json`：`{cpu_ratio, mem_ratio, io_ratio, net_ratio, notes}`
（guest 合计 / host 总量，均 ≤ 1 为预期）。

注意：SSH 探针的读延迟高于本地共享文件读取，offset 取中位数可降低抖动影响；
分析测量质量时应把 SSH 传输开销纳入说明，不能与 virtiofs 探针延迟混为一谈。

由 `run-episode` 在导出/完整性校验通过后自动生成，无需单独命令。

### H3 验收

1. `clock_sync.json` 的 `offset_seconds` 非空、`probe_count ≥ 1`（心跳缺失时
   记 null，reconciliation 注明 skip）；
2. guest 事件映射到 host 时间线后，`reconciliation.json` 的
   `cpu_ratio/io_ratio/net_ratio/mem_ratio ≤ 1`（内存口径不同允许偏差，见
   `notes`；超出阈值进 `quality` 留待 H4）。

## H4：基线 + reconciliation + 质量记录

空 VM 空转采样产出 `baseline_<profile_id>.json`（与 vm_samples 同构的速率/峰值/
memory-time/coverage 摘要），把 `baseline_profile_id` 回写 run_manifest；episode
对账比值与阈值比较，超阈值偏差、缺失指标、coverage 事故写入 `episode.json`
的 `quality`。

```bash
# 先测基线（空 VM 空转）
mini-swe-harness baseline --config <真机 VM 配置>.json --run-dir runs/h4-001 --duration 60

# 再跑 episode（自动读取 run_manifest.baseline_profile_id 写入 quality）
mini-swe-harness run-episode --manifest <真机 manifest>.json --config <真机 VM 配置>.json --run-dir runs/h4-001 --image-map examples/image_map.jsonl
```

阈值默认：`cpu/io/net_ratio ≤ 1.0`，`mem_ratio ≤ 1.5`（内存口径不同放宽）、
`coverage_gap ≤ 3`；超阈值记 `level: warn`，缺失记 `level: info`。阈值可经
`quality.QualityThresholds` 覆盖。`quality` 同时记录 `baseline_profile_id` 与
生效阈值，供后续分析识别不可信数据点。

### H4 验收

1. 空 VM 基线文件 `baseline_<profile_id>.json` 生成，`run_manifest.baseline_profile_id`
   回写；
2. episode 对账比值超过阈值（或缺失/coverage 事故）时，对应条目写入
   `episode.json` 的 `quality.entries`。

## H5：并行调度 + guardrail

`run-episodes` 按 ``GuardrailPolicy.max_concurrency`` 限制同时运行的 VM 数；每次
接入新 episode 前读宿主机快照（MemAvailable + 磁盘可用空间），低于下限时按
策略排队等待（默认）或直接拒绝（`--no-queue`），绝不静默超载。事件隔离由
``run_episode`` 的独立 episode 目录保证。

**并发网络槽位**：每个并发 VM 必须独占一套 ``tap_device``/``guest_ip``/
``guest_mac``（一个 tap 只能挂一个 VMM）。`run` 接受每槽位一份 ``--config``
（可多次）；只给一份时按 ``--concurrency`` 与 ``--ip-mode`` 用
``config.derive_slot_config`` 自动派生（`last_octet`=bridge 汇聚 /24，默认；
`third_octet`=每 tap 一个 /30）。config 池内三元组重复在起跑前报错；槽位数
小于 ``--concurrency`` 时由槽池节流。episode 完成即回收槽位（done callback），
episode.json / run_manifest 落 ``slot_index`` 与 ``vm_tap_device``/
``vm_guest_ip``/``vm_guest_mac``，事后可把测量数据对回具体 tap/IP。

``--manifest`` 可多次传入，且每次可空格分隔多个文件（shell 通配符
``--manifest manifests/*.json`` 直接可用）；也可用 ``--manifest-dir <dir>`` 一次
加入目录内全部 ``*.json``：

```bash
# 一次性：生成槽位配置 + 打印宿主机 tap/bridge 准备命令（在宿主机执行输出的命令）
mini-swe-harness make-configs --base vm_base.json --slots 4 \
    --ip-mode third_octet --out-dir configs/

# 批量 run：多份 config（与 --concurrency 一一对应）
mini-swe-harness run \
    --config configs/vm_config.slot0.json --config configs/vm_config.slot1.json \
    --run-dir runs/h5-001 \
    --image-map examples/image_map.jsonl \
    --manifest m1.json m2.json m3.json \
    --concurrency 2 --memory-floor 12 --disk-floor 50

# 或：单份 config 自动派生（等价于上面 make-configs + 多份 --config）
mini-swe-harness run \
    --config vm_base.json --ip-mode third_octet \
    --run-dir runs/h5-001 --image-map examples/image_map.jsonl \
    --manifest m1.json --manifest m2.json --manifest m3.json \
    --concurrency 2 --memory-floor 12 --disk-floor 50
```

guardrail 口径沿用 `gpu_tasks/container.py` 的 POLICY 思路：内存/磁盘可用量低于
下限时暂停接入；读数缺失（非 Linux）不阻断（横向前提③）。排队超时或
`--no-queue` 时该 episode 落 `status: rejected`（`episode.json` + run_manifest）。

### H5 验收

1. 并发不超上限（N 个 episode 同时运行 ≤ `concurrency`）；
2. 宿主机内存/磁盘触下限时排队或拒绝，无静默超载（结果含 `rejected` 与原因，
   排队超时也记录）。

## prepare：批量准备（import + manifest + 覆盖校验）

把 ``<traces-dir>/<benchmark>/<task_id>/`` 下的全部 `trajectory.json`（兼容
`runs/<run_id>/...` 嵌套布局，一 task 多 run 时每个 run 一条源 episode，多余
run 加 `--rN` 后缀）批量 import 进 source store，并为每个 (episode, attempt)
生成 harness episode manifest。

**store 布局为每源 episode 一份**：``<store-dir>/<benchmark>/<source_episode_id>/``，
每个 store 只含一条源 episode（id 为 ``{benchmark}--{task_id}``，多 run 加
`--rN`；跨 attempt 共享一份源）。episode 注入会把 ``manifest.source_store``
整个上传进 guest（VM 每 episode 新建即销毁，无法复用），per-episode store 让
每条 episode 只搬运自己需要的数据（KB~MB 级），避免全量 benchmark store
（GB 级、海量小文件）反复 scp 超时。manifest 的 `episode_id`留空，harness 自动
生成 ``{benchmark}--{task_id}--a{attempt}``，与 source_episode 必然不同（避免
replay 在同 store 内自引用撞主键）。

```bash
# 先 dry-run 看报告（不写任何文件）：覆盖缺口会在起跑前暴露
mini-swe-harness prepare --traces-dir mini-swe-traces --image-map tasks.jsonl \
    --store-dir stores --manifest-dir manifests --dry-run

# 正式执行（幂等，可反复跑；import 委托 mini-swe-agent 的 replay import）
mini-swe-harness prepare --traces-dir mini-swe-traces --image-map tasks.jsonl \
    --store-dir stores --manifest-dir manifests

# 之后直接批量 run（串行用 --concurrency 1；--manifest-dir 加入目录内全部 manifest）
mini-swe-harness run --config vm_base.json --ip-mode third_octet \
    --registry <harbor> --run-dir runs/batch-001 --image-map tasks.jsonl \
    --manifest-dir manifests --concurrency 4
```

报告（stdout JSON）含：`store_layout`（固定 `per_episode`）、`missing_image`
（映射表缺条目的 task，不进 manifest）、`no_trajectory`（缺 trajectory.json 的
目录）、`imported`/`existing`（幂等跳过）、`import_failed`（单条失败不拖垮批量，
CLI 以非零退出）。`--skip-import` 只做校验与 manifest 生成；`--attempts N` 每
task 生成 N 份 attempt manifest；`--profile-digest` 写入源 episode metadata 与
manifest（A4 digest 校验）。

### prepare 验收

1. dry-run 报告 `missing_image` 与实际映射表缺口一致；
2. 重复执行第二次全部落入 `existing`，store 无重复 episode；
3. 生成的 manifest 能被 `run-episode`/`run` 直接消费（source_episode 在
   store 内存在、episode_id 与 source_episode 不同）。

## 实施步骤（计划文档 §5.3）

- [x] **H0 单 VM 冒烟**（2026-10-16 完成实现；按用户要求未做动态测试，真机验收待 Linux/KVM 环境）
- [x] **H1 采样器**（2026-10-16 完成实现；伪造 proc 树 + 注入时钟桩冒烟通过，含已知负载下界断言的静态版本；stress-ng 真机验收测试已写（`test_stress_ng_lower_bounds_host`），待 Linux 环境执行）
- [x] **H2 episode 注入 + 镜像预热**（2026-10-16 完成实现；按 §5.4 主方案重构为映射表 + Harbor tag/digest 解析 + override 直写 Harbor 路径，假 VM + 假 exec + 假 Harbor 桩冒烟通过，覆盖覆盖校验/歧义跳过/预热/注入/导出/final 校验与失败路径；真机验收待 Linux/KVM 环境）
- [x] **H3 时钟校准 + 双层 join**（2026-10-16 完成实现；`clocksync.py` + `join.py` + episode 集成，假心跳/假 guest_events/假 vm_samples 桩冒烟通过，覆盖 offset 中位数/drift_ppm/去重/窗口过滤/差值比值 ≤1；真机校时与对账验收待 Linux/KVM 环境）
- [x] **H4 基线 + reconciliation + 质量记录**（2026-10-16 完成实现；`baseline.py` + `quality.py` + `host.py` + episode 集成 + `baseline` 子命令，假 VM/假采样器/合成样本桩冒烟通过，覆盖速率/峰值/积分汇总、基线文件生成与 run_manifest 回写、超阈值/缺失/coverage 质量条目；真机空转基线验收待 Linux/KVM 环境）
- [x] **H5 并行调度 + guardrail**（2026-10-16 完成实现；`guardrail.py` + `scheduler.py` + `run` 子命令，假 snapshot/假 run_episode_fn 桩冒烟通过，覆盖并发上限、内存/磁盘下限拒绝、排队后接入、排队超时、runner 异常落盘；真机多 VM 并发验收待 Linux/KVM 环境）
- [x] **prepare 批量准备**（2026-10-19 完成实现；`prepare.py` + `prepare` 子命令，假 importer 桩冒烟覆盖扫描/覆盖校验/幂等/多 run 命名/失败隔离；已对 mini-swe-traces 真实数据 dry-run 验证：4678 条 trace、7 个 benchmark store、暴露 41 个 terminalworld 映射缺口；真实 import 端到端验证通过）
- [x] **H5a 并发网络槽位**（2026-10-19 完成实现；`config.derive_slot_config` + scheduler config 池/槽位分配（done callback 回收）+ `run` 单 config 自动派生 + `make-configs` 子命令，桩冒烟覆盖槽位互斥/池冲突拒绝/槽池节流/越界报错；真机多 tap 验收待 Linux/KVM 环境）
