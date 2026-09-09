# MindIE Motor — Agent Guide

> 给 AI 代理的仓库级说明。本文件在会话启动时自动加载，只放「如何安装 / 构建 / 测试 / 提交」的操作事实；
> 深度架构与开发流程见 `.agents/skills/motor-dev/`（按需加载，不在此重复）。

## 项目简介

MindIE Motor 是面向大模型（LLM）分布式推理的控制器系统：Controller 管理实例生命周期，Coordinator 负责请求调度（PD 分离），NodeManager 直接管理 vLLM/SGLang 原生引擎进程，KV Conductor（Rust）提供 KV 缓存感知路由。

## 仓库结构

```text
motor/                   Python 源码（coordinator / controller / node_manager / common / config）
motor/kv_conductor/      Rust KV Conductor（axum + tokio + ZMQ）
tests/                   测试（目录镜像 motor/ 结构）
examples/                部署配置示例（user_config.json、deployer）
pre-commit/              pre-commit 钩子脚本（check_header、check_modern_typing 等）
scripts/                 构建辅助脚本（generate_proto.sh 等）
deploy/                  MotorJob CRD
docs/                    文档站（mkdocs）
```

## 环境要求

- Python **3.10+**（类型语法按 py310 目标；打包/运行见 `setup.py` `>=3.11`）
- Rust + Cargo：`libmindie_workload_shm.so` **必需**（缺 `.so` 且无 cargo 时 `build.sh` 会 rustup 安装）；缺 `.so` **禁止出包**。已有 `lib/*.so` / `bin/kv-conductor` 则跳过 cargo。kv-conductor 缺二进制且有 cargo + libzmq 时一并打进 wheel；缺 libzmq / pkg-config 时自动跳过（也可 `SKIP_KV_CONDUCTOR_BUILD=1`）

## 安装

```bash
# 1. 依赖（whl 的 install_requires 为空，必须显式装 requirements.txt）
pip install -r requirements.txt

# 2. 构建并安装 whl（已有 so/二进制则跳过 cargo；workload-shm 必需）
bash build.sh
pip install dist/motor-*.whl
```

## 构建

```bash
bash build.sh                                      # 已有 lib/*.so / bin/kv-conductor 则跳过 cargo；缺了才编
SKIP_WORKLOAD_SHM_BUILD=0 bash build.sh             # 改过 .rs：强制重编 workload-shm
SKIP_KV_CONDUCTOR_BUILD=0 bash build.sh             # 改过 conductor：强制重编（官方 Dockerfile 默认如此）
SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh             # 显式跳过 kv-conductor cargo（bin 缺失则省略该 crate）
SKIP_RUST_BUILD=1 bash build.sh                     # 两个 SKIP=1 快捷键（未显式设置时才填充）
WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so bash build.sh   # 离线预编译 .so
```

产物：`dist/motor-3.1.0-py3-none-x86_64.whl`（ARM 为 `aarch64`）。验收：

```bash
unzip -l dist/motor-*.whl | grep -E 'kv-conductor|libmindie_workload_shm.so'
```

`libmindie_workload_shm.so` 必须存在；缺库时 `build.sh` 在 `pip wheel` 前 `exit 1`，若 archive 仍缺该成员则删除刚打出的 wheel 并报 `refusing to emit/keep`。kv-conductor 仅在本次编出二进制时强制打进 wheel。

前置：仓库根目录、Python 3.11+、已装 `requirements.txt`、`curl`、C/C++ 编译器（Ubuntu：`build-essential` / `g++`；openEuler：`gcc gcc-c++ make`）。打 kv-conductor 还需要 **g++**（`zmq-sys` 找 `c++` 工具）以及 libzmq + pkg-config（Ubuntu：`libzmq3-dev pkg-config`；openEuler：`zeromq-devel pkgconf`）。缺产物且无 cargo 时 `scripts/ensure_rust.sh` 会 rustup 安装（默认 rsproxy）；编 kv-conductor 时同一脚本探测 `c++`/`g++`，缺失则装 `g++`（`SKIP_CXX_INSTALL=1` 关闭）。`.so` 必须在与运行镜像相同的 OS/glibc 里编译。

- 自动执行 `scripts/generate_proto.sh`（etcd protobuf）。默认：**已有** `libmindie_workload_shm.so` / `bin/kv-conductor` 则跳过对应 cargo；**缺文件才编译**。无可用 cargo 时 **任何** Rust 都编不了：workload-shm 硬失败（除非 `WORKLOAD_SHM_PREBUILT` / 已有 `lib/*.so`）。先处理 `motor/coordinator/workload_shm_rs`（`libmindie_workload_shm.so`，**不可缺**）。再尝试 `motor/kv_conductor`：仅 conductor 特有原因 WARNING 跳过——缺 libzmq、缺 g++（`zmq-sys`）、或 conductor 自己的 `cargo build` 因 zmq 链接失败；**不要**把「没有 cargo」写成 conductor 跳过
- **cargo / g++ / zmq 探测**：`scripts/ensure_rust.sh` 先 source `$HOME/.cargo` / `CARGO_HOME`（Jenkins PATH 经常看不到 rustup），仍没有且确实需要编译时才 rustup 安装（默认 rsproxy，失败重试，再回退官方源；`SKIP_RUST_INSTALL=1` 关闭）。必须 `cargo --version` 成功才算可用：rsproxy 503 回滚后会留下无 default toolchain 的 cargo 代理，不能继续 `cargo build`。编 kv-conductor 前探测 `pkg-config --exists libzmq` 或 `zmq.h`（缺 pkg-config 不致命），再保证 `c++`/`g++`（`SKIP_CXX_INSTALL=1` 关闭自动装 g++）。**不**在 `build.sh` 里 apt 安装 libzmq。CI / 镜像构建请用干净树，或显式 `SKIP_WORKLOAD_SHM_BUILD=0`（官方 Dockerfile 已默认 0），避免把宿主机 `.so` 打进 Ubuntu 镜像
- **SKIP**：默认有产物就跳过 cargo。`SKIP_WORKLOAD_SHM_BUILD=0` / `SKIP_KV_CONDUCTOR_BUILD=0` 强制重编（`${VAR:-0}` 会把 unset 当成 0，判断强制重编必须区分「未设置」与「显式 0」）。`SKIP_KV_CONDUCTOR_BUILD=1` 永不编 conductor（无 bin 则省略）。缺 libzmq/pkg-config 时同样自动跳过 conductor。`SKIP_RUST_BUILD=1` 是前两者 `=1` 的快捷键（未显式设置时才填充，例如 `SKIP_RUST_BUILD=1 SKIP_KV_CONDUCTOR_BUILD=0` 可只重编 kv-conductor）；`SKIP_RUST_INSTALL=1` 禁止 rustup，须 `WORKLOAD_SHM_PREBUILT` 或已有 `lib/*.so`。禁止打出无 `.so` 的 wheel
- **ABI**：Euler 宿主机编完塞进 Ubuntu 镜像可能加载失败。Dockerfile 路径在镜像内 `bash build.sh` 后卸掉 rustup
- **源码开发（Python）**：改代码直接生效（import 走源码目录），无需重建
- **源码开发（Rust）**：改 `.rs` 后必须重新编译（`SKIP_WORKLOAD_SHM_BUILD=0 bash build.sh` 或 crate 内 `cargo build --release`）；`native.py` 优先加载 `workload_shm_rs/lib/*.so`（whl）再回退 `target/release/*.so`，缺失时抛 `NativeWorkloadShmUnavailable`（不静默回退错误账本）
- **pip 源**：`build.sh` 默认清华 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。该源 403 时设 `PIP_INDEX_URL`（镜像 Dockerfile 默认华为云）；隔离拉 setuptools 失败会自动 `--no-build-isolation` 重试
- **打包/部署**：whl 是快照，打包后才装的镜像/环境必须重新 `bash build.sh` 生成新 whl，否则旧 wheel 残留导致 NameError/ImportError

## 测试

**只用 `bash tests/run_tests.sh`，不要直接 `python -m pytest`。**

```bash
# 渐进式：单文件 → 模块 → 全量
bash tests/run_tests.sh tests/coordinator/test_xxx.py
bash tests/run_tests.sh tests/coordinator/
bash tests/run_tests.sh

# 常用选项：-v 详细 / -s 显示输出 / -x 失败即停 / -n NUM 并行（默认 6）
#           --serial 串行 / --cov 覆盖率 / -k "关键词" 过滤
bash tests/run_tests.sh --cov tests/
```

- 测试目录镜像源码结构：`motor/config/foo.py` → `tests/config/test_foo.py`
- 写测试前先读 `.agents/skills/motor-dev/references/testing-guide.md` 的四条设计原则

## 代码风格（pre-commit 强制）

提交前必须通过 `pre-commit`（或 `pre-commit run --all-files`），钩子：

- **ruff**（line-length 120, target py310）+ **pylint** + **bandit**
- **check-header**：每个 Python 文件必须有 Mulan PSL v2 license 头（文件第一行）
- **check-modern-typing**：强制 Python 3.10+ 原生类型语法（`X | None` 而非 `Optional[X]`、`dict[K,V]` 而非 `Dict[K,V]`），`deployer/` 除外
- **typos / codespell / gitleaks / check-yaml / trailing-whitespace** 等基础检查
- Rust（kv_conductor）：cargo fmt + cargo clippy `-D warnings`

日志规范：`logger.info("msg %s", var)` —— **禁止 f-string**（延迟格式化）。

## 提交规范

- Commit message 格式：`[tag] 中文描述`（tag: `fix` / `feature` / `refractor` / `docs` / `skill` / `bugfix`）
- 每个 `motor/` 改动必须附带测试
- PR 描述按 `.gitcode/PULL_REQUEST_TEMPLATE.md`

## Agent Skills

- 仓库 Skill 的唯一权威目录是 `.agents/skills/`。普通自然语言请求先进入对应父 Skill；
  用户显式指定 `$motor-...` 原子 Skill 时可以直接使用，但原子 Skill 定义了父路由入口
  约束时仍须先满足该约束。
- 三个父 Skill（`motor-deploy`、`motor-validation`、`motor-diagnosis`）默认参与隐式
  触发；已提供 `agents/openai.yaml` 的原子 Skill 设置
  `policy.allow_implicit_invocation: false`。暂未提供该元数据的原子 Skill 由父 Skill
  通过仓库相对路径加载。
- 修改 `motor/`、`tests/` 或开发文档：使用 `motor-dev`。
- 拉起、部署、重启、停止、部署前检查、配置校验或替换 wheel：先使用
  `motor-deploy`，由它路由到部署原子 Skill。
- 部署后的 readiness、功能、accuracy、benchmark 或性能分析：先使用
  `motor-validation`，由它路由到验证原子 Skill。
- deploy/startup/runtime 异常、性能目标未达标且原因未知、日志采证或根因定位：先使用
  `motor-diagnosis`，由它保存证据并路由到诊断原子 Skill。
- 预检和 dry-run 不授权修改配置或集群；配置修改、apply、restart、stop 和远端
  `boot.sh` 修改必须针对具体目标获得明确授权。

## 开发技能（AI 辅助开发必读）

深度开发规范在 `.agents/skills/motor-dev/`（支持的 agent 可显式调用 `$motor-dev`，或按需读取目录）：

- `SKILL.md` — 硬性约束（测试伴随改动、run_tests.sh、license、类型语法）、渐进式测试工作流、Skill Sync 铁律（**发现文档与代码不符必须同步更新并同 PR 合入**）
- `references/<module>.md` — 各模块架构（Coordinator/Controller/NodeManager/Metrics/KV Conductor）
- `bug-fix-history/INDEX.md` — 持续学习案例索引（调试前先查）
- `references/issue-reporting.md` — 定位问题后按模板提交 ISSUE（仅用户同意后加载）

集群级 Pymotor 验收套组（扩缩容 / RAS / GPQA / 性能，非 `tests/e2e/` 进程内测试）见
`.agents/skills/motor-smoke-suite/`。用例执行分别路由到
`motor-scale`、`motor-reliability`、`motor-validation-benchmark`（GPQA 精度合同在套组内）。
无人值守 profile 放在本机 ignored 目录 `.motor-local/suite-profiles/`。
