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

- Python **3.10+**（类型语法按 py310 目标）
- Rust + Cargo（构建 kv_conductor，可选：存在预编译 bin 时可跳过）

## 安装

```bash
# 1. 依赖（whl 的 install_requires 为空，必须显式装 requirements.txt）
pip install -r requirements.txt

# 2. 构建并安装 whl
bash build.sh                    # 生成 dist/motor-*.whl（自动生成 protobuf + cargo 构建 kv_conductor）
pip install dist/motor-*.whl
```

## 构建

```bash
bash build.sh        # 产物：dist/motor-0.1.0-py3-none-any.whl
```

- 自动执行 `scripts/generate_proto.sh`（etcd protobuf）与 kv_conductor 的 cargo 构建
- 无 cargo 时用 `SKIP_KV_CONDUCTOR_BUILD=1` 跳过（使用预编译 bin）
- **源码开发**：改代码直接生效（import 走源码目录），无需重建
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
