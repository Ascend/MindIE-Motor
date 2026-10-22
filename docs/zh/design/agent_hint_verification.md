# Agent Hint 验证记录

> 关联设计：[Agent Hint 推理侧支持](agent_hint.md)

## 1. 验证目标与范围

| 目标 | 映射设计 | 判据 |
| --- | --- | --- |
| spec/legacy 协议解析与通道边界 | §2、§4.1 | 全字段落位、未知字段进 raw_extra、畸形 WARN 丢弃、body 通道拒绝 spec 键 |
| 会话亲和角色隔离/epoch/parent | §4.2 | P/U/D 命名空间互不可见、epoch 失配失效、parent 传播、LRU 边界、env 开关 |
| fresh 均衡放置 | §4.3 | 三元组各键生效、burst 确定性摊匀、eligible 空回退、共享表计数 |
| 软优先级准入 | §4.4 | 预留消费语义（普通/高优/边界）、热更新重算、header 提取、门槛过滤与兜底 |
| strict_priority 信任机制 | §4.5 | 可信/剥离/伪造路径、配置校验、旁路与豁免行为 |
| agent_status 带外通道 | §4.6 | 上报迁移契约（乱序容忍/finalize 幂等/终态不可逆）、结构错误 400、字段畸形 200+applied=false、finalize 跨进程释放 pin |
| cache_control 声明生命周期 | §4.7 | 显式 TTL 入账、到期释放、保活 recency、finalize 协同不二次释放、TTL 钳制、容量驱逐不动作、ttl_explicit 不序列化 |
| **验收边界（affinity TTL）** | §4.7 | **本阶段验收口径为 affinity TTL（声明账本 + pin 保活/到期/释放标签），不含真实引擎 KV 驻留/驱逐效果**；块级 retain/offload/prefetch 为后续能力（需引擎能力协商与完成反馈），届时补充缓存压力、TTL 内外复用、父子共享前缀、finalize 实机用例并观察 cached tokens、重复 prefill、真实驻留与 P99 TTFT（多 worker / DP / P-D 拓扑分别验证） |
| 兼容性 | §5、§6 | 默认全部关闭时基线行为零变化；既有配置/客户端不受影响 |

## 2. 测试资产

| 测试文件 | 覆盖 |
| --- | --- |
| `tests/coordinator/domain/test_agent_hint.py` | spec schema、cache_control（pin/ttl_s/range unit 收敛）、ttl_explicit、畸形输入 |
| `tests/coordinator/domain/test_agent_hint_wire.py` | header 解码、spec→legacy 映射、body wins、golden vector |
| `tests/coordinator/domain/test_agent_session_status.py` | 状态机迁移契约、TTL、LRU |
| `tests/coordinator/domain/test_block_offset_translator.py` | 块坐标换算与 None 守卫 |
| `tests/coordinator/domain/test_cache_declaration.py` | 声明表语义：入账/钳制/惰性过期入队/sweep/renew/finalize/容量驱逐/env 构建 |
| `tests/coordinator/middleware/test_rate_limiter.py` | 预留额度、strict 旁路、热更新 |
| `tests/coordinator/scheduler/runtime/__init__.py` + `test_session_affinity.py` | 角色/epoch/parent/LRU/env |
| `tests/coordinator/scheduler/runtime/test_session_affinity_shared.py` | 共享表语义、drop_all_roles、count_pins_by_instance |
| `tests/coordinator/scheduler/runtime/test_agent_status_channel.py` | notify/PUB 分支、finalize drop 联动 |
| `tests/coordinator/scheduler/runtime/test_cache_lifecycle.py` | keeper 集成：门控、never-raise、sweep→drop、touch、任务启停 |
| `tests/coordinator/scheduler/test_scheduler_client.py` | pin 解析集成、门槛过滤、balanced 放置 |
| `tests/coordinator/scheduler/test_cache_lifecycle_e2e.py` | 声明→保活→到期释放→finalize 全链路 |
| `tests/coordinator/api_server/test_agent_status_endpoint.py` | 端点协议：400/200+applied=false、GET、skip_paths |
| `tests/coordinator/core/test_config.py` | 新增配置段校验与默认值 |
| `tests/coordinator/core/test_config.py`（既有） | Ascend 侧 decode_first_token_timeout 等既有用例保持 |
| `tests/e2e/test_standalone_coordinator_smoke.py` | standalone coordinator 冒烟 |

## 3. 执行环境

- 容器化环境：openEuler 24.03 + Python 3.11（与门禁编译/UT 镜像口径一致），
  `TORCH_DEVICE_BACKEND_AUTOLOAD=0`（无 NPU 透传的开发机跳过 torch_npu 后端加载），
  `bash tests/run_tests.sh`（pytest 封装，-n 6 并行）。

## 4. 执行记录

### 4.1 单元与集成测试

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| Coordinator 模块 | `bash tests/run_tests.sh tests/coordinator/` | **2233 passed / 0 failed** / 37 skipped |
| 全仓回归（合入前基线） | `bash tests/run_tests.sh` | 4316 passed / 0 failed / 42 skipped |
| 全仓回归（并入上游 `49e0595f` 后终版） | `bash tests/run_tests.sh` | **4387 passed / 0 failed** / 46 skipped |

（执行时间 2026-10-19；warnings 为存量告警，与本次改动无关。终版分支已并入合入窗口期
上游新增的 `49e0595f`（flash engine KV offload 事件 + DP 统计修复），冲突解决：
`scheduler_client.py` scheduled 日志行采用上游新标签 + 本侧 `prio/aff/reason/sp` 字段并集；
`scheduler_connection_manager.py` 采用上游 `log_dp_stats` 新语义 + 本侧优先级门槛参数。）

### 4.2 静态检查

针对本次变更的全部 32 个 Python 文件，按仓库 pin 版本执行：

| 检查 | 版本/配置 | 结果 |
| --- | --- | --- |
| ruff check | 0.14.14 + `pre-commit/pyproject.toml` | **0 error** |
| ruff format --check | 同上 | 全部已格式化 |
| pylint | v4.0.5 + `pre-commit/pyproject.toml` | **exit 0**（0 findings） |
| bandit | 1.9.4 + `pre-commit/pyproject.toml` | **exit 0**（B105×2 头名常量、B108 `/dev/shm` 以 `# nosec` + 理由注释豁免） |
| check_modern_typing `--check` | 仓库钩子 | 通过 |
| check_header | 仓库钩子 | 通过（2 个文件删除冗余 `# -*- coding -*-` 行，Mulan 头置于首行） |

gitleaks 离线二进制在开发环境不可用，本次未执行（门禁 CI 覆盖）。

### 4.3 端到端验证（轻量：真实 Coordinator 进程 + mock 引擎）

在 openEuler 24.03 容器内从源码树拉起**真实 Coordinator 进程**（`python -m
motor.coordinator.main`，含 InferenceWorker 子进程与 workload-shm 原生库），对端为
OpenAI 兼容 mock 引擎；开关全开：`MOTOR_SESSION_AFFINITY=1`（SHARED 共享表 +
`PLACEMENT=balanced`）、`MOTOR_CACHE_CONTROL_LIFECYCLE=1`（sweep 1s）、
`agent_status_config.enabled=true`、`strict_priority_config` 启用。驱动以 HTTP 驱动
mgmt/infer 两个面，**24/24 检查全部通过**：

| 场景 | 判据（实测日志/响应原文） |
| --- | --- |
| 启动与拓扑 | readiness false → SET P-only → readiness true |
| agent_status 带外通道 | `tool` 上报 `{"applied": true, "previous_state": null}`；`GET /agent-status?session_id=s-1` 返回 `state=tool` + tools + transition_count |
| 会话亲和 fresh→hit | 两次同会话请求：`aff=fresh` → `aff=hit` |
| balanced 放置 | affinity INFO 行 `placement=balanced` |
| 父会话传播 | `session affinity parent session_id=s-2 parent_session_id=s-1` + `aff=parent` |
| finalize 释放 pin | finalize applied（tool→finalize）后同会话请求 `aff=fresh` |
| 优先级观测与信任门 | scheduled 行 `prio=3 aff=fresh reason=e2e-priority sp=3`（携带 `X-Strict-Priority-Token` + hint `strict_priority`） |
| 声明生命周期 | 显式 `ttl_s=60` 声明入账；约 60s 后 keeper 日志 `retention elapsed; released session pin: session_id=s-ttl ... dropped=1`；后续请求 `aff=fresh` |
| 协议边界 | 7 次 chat 调用 mock 引擎均未收到 `X-Agent-Hint` 头（header 不透传引擎） |

已知非缺陷项：Coordinator ObsServer 对 mock 引擎 `/metrics` 输出 Prometheus 解析
ERROR（mock 未实现 metrics 端点，真实 vLLM 引擎不受影响）。

### 4.4 端到端验证（完整：docker 部署真实 vLLM 引擎 + 真实推理流量）

在本机（MindCluster 单节点，Atlas 800I A2 8×910B4，驱动 26.1.1）以 `docker_deploy.py`
拉起双容器拓扑，对 **真实 vLLM 引擎（vllm-ascend 0.23.0rc1 / Llama3-8B-Chinese-Chat，
device 1）** 发起真实推理流量，agent-hint 全链路 **23/23 检查通过**：

| 项 | 内容 |
| --- | --- |
| 拓扑 | `motor-mgmt`（Coordinator + Controller，3 InferenceWorker）+ `motor-union-u0`（NodeManager + vLLM 引擎，union 角色） |
| 镜像 | `mindie-motor-vllm:master`（本分支源码构建，CANN 9.0.1 底座） |
| 真实推理 | `/v1/chat/completions` 返回真实模型生成（中文自述回复），`scheduled role=union` 调度日志正常 |
| agent-status | tool 上报 `applied=true` → GET 查询状态机（transition_count）→ finalize 释放亲和 |
| 会话亲和 | `aff=fresh → hit → parent → finalize 后 fresh`（调度日志原文） |
| balanced 放置 | `placement=balanced`（affinity INFO 行） |
| 优先级 | `prio=3 ... reason=e2e-priority sp=3`（X-Strict-Priority-Token 信任门命中） |
| 声明生命周期 | 显式 `ttl_s=60` 入账 → keeper `retention elapsed; released session pin ... dropped=1` → 后续请求 `aff=fresh` |
| 协议边界 | 引擎侧未收到 `X-Agent-Hint` 头（0 泄露） |

与轻量 e2e 的差异：引擎为真实 vLLM 多进程（APIServer + EngineCore + NPU worker），
请求经 NodeManager 注册的实例端点完成真实 prefill+decode。

**评审修复后的复验**：将 §4.5 全部修复（tombstone/MSAFF5 布局/声明承诺写槽/
keeper 条件释放与限量/信任门热更新/脱敏/状态机扇出）构建进镜像重新部署后，
同一 23 项驱动 **23/23 通过**；其中跨 worker GET 404 的偶发视图差异（设计 §4.9
已声明限制）与 keeper 释放日志在本轮均确认正常。

### 4.5 评审修复（tobking，2026-10-19）

12 条检视意见的处置记录（合理即修，边界类以文档显式化回应）：

| 评审 | 处置 |
| --- | --- |
| 194526225 / 194526268（验收边界：affinity TTL 非 KV retention 闭环） | 采纳。§4.7 更名 affinity TTL 并显式声明边界（设计 §1 判据表同步）；真实 KV retention 为后续能力，验收扩展计划见 §1 边界行 |
| 194526933（prediction 嵌套工具构造 500） | **修复**：`PredictionTool` 构造纳入异常边界（WARN + 丢 entry）；回归 `test_malformed_prediction_tool_entry_never_raises` |
| 194527501（1e999→inf→OverflowError 击穿容错） | **修复**：全部数值入口统一 `_finite_int`（非有限数拒绝 + OverflowError），覆盖 priority/strict/cache_control range/prediction/agent_status/session/context_edit；回归 `test_non_finite_numbers_never_raise`（10 入口参数化） |
| 194527742（清零删除截断探测链） | **修复**：删除改 tombstone（探测链完整、插入优先复用）；回归 `test_drop_keeps_probe_chain_reachable_for_colliding_sessions` |
| 194527795（session id 截断后不可查/不可清理） | **修复**：槽位 session 区改存 sha256 摘要标签，任意长度/多字节 id 全程一致；回归 `test_long_and_multibyte_session_ids_roundtrip` |
| 194527836（全表扫描阻塞事件循环） | **修复**：共享表新增 per-(role, instance) 增量计数区（attach 对账），`count_pins_by_instance` O(1)；keeper 释放按 `_SWEEP_RELEASE_BATCH=64` 限量分轮；回归 `test_count_pins_by_instance_tracks_rebind_and_renewal`、`test_sweep_release_is_batch_bounded` |
| 194528015（finalize 终态仅单 worker 成立） | **修复**：PUB 扇出经 `agent_status_state_listener` 驱动各 worker 本地状态机（终态不可逆跨 worker；listener 故障不影响 pin 释放）；GET 仅本进程记录的限制保持并已在设计 §4.9 声明；回归 `test_pub_events_drive_local_state_machine`、`test_pub_state_listener_failure_never_breaks_pin_release` |
| 194528088（带外写入鉴权与归属边界） | **修复**：`agent_status_config.require_infer_auth`（默认 false 保持 harness 契约）为 true 时端点继承 API-key 与限流门（skip 集移除）；部署约束写入设计 §4.9；回归 `test_agent_status_require_infer_auth_removes_skip_paths` |
| 194528148（strict 信任快照热更新失效） | **修复**：middleware live 读 holder、`_apply_config_changes` 同步刷新；回归 `test_strict_trust_config_hot_reload_takes_effect_immediately`（禁用/轮转立即失效） |
| 194528212（strict token 进 DEBUG 日志） | **修复**：`filter_sensitive_headers` 名单补 `x-strict-priority-token`；回归 `test_filter_sensitive_headers_covers_strict_token` |
| 194528478（旧 TTL 误删他 worker 续期的共享 pin） | **修复**：声明改在 pin 建立后记录，并把保留承诺到期时间（wall）写入共享槽位（max-merge，MSAFF5 布局）；keeper 过期释放前对比槽内承诺与本地声明到期——槽内承诺更晚则移交续期 worker 的 keeper（不无限 defer，LRU/stale 兜底保留）；回归 `test_renewal_stamp_only_advances_on_record`、`test_expired_declaration_defers_release_when_pin_renewed_elsewhere` |

### 4.6 合入前修复的质量问题（同轮静态门禁）

- `tests/coordinator/scheduler/test_cache_lifecycle_e2e.py`：F811（参数遮蔽导入的
  fixture）改为 `@pytest.mark.usefixtures("native_lib")`；D209 文档字符串收尾；
- `tests/coordinator/domain/test_agent_hint_wire.py`：W0404（函数内重复 import base64）；
- `tests/coordinator/scheduler/runtime/test_session_affinity_shared.py`：W0404（子进程
  目标函数内重复 import，模块级导入已覆盖 fork/spawn 两种启动方式）；
- `session_affinity.py` ↔ `session_affinity_shared.py` 模块级循环引用（R0401）：
  `SessionPin` 与 `_DEFAULT_MAX_ENTRIES` 下移至 `session_affinity_shared.py`
  （共享表与进程内表共用同一类型，且该模块仅依赖 stdlib），`session_affinity.py`
  单向引用并 re-export，依赖方向唯一化；行为与对外 API 路径不变；
- `fastapi_middleware.py`：R1714 合并等值比较为 `in`；
- `scheduler_client.py`：W0706 删除冗余 `except asyncio.CancelledError: raise`
  （3.8+ CancelledError 为 BaseException，`except Exception` 不会捕获）。

### 4.7 评审修复（PR #1196 检视意见补充）

| 评审 | 处置 |
| --- | --- |
| 共享表 `record_balanced` 替换到另一实例时只减旧实例计数、未给新实例加计数，共享 pin 计数漂移放大 balanced 倾斜 | **修复**：替换分支改为先 `-1` 旧实例再 `+1` 新实例（与 `record` 一致）；回归 `test_record_balanced_replacing_moves_counter_to_chosen` |
| `SimpleRateLimiter.update_config` 只重算 `max_requests`/`window_size` 派生的 reserve，`priority_reserve_ratio` 热更新不生效，`inference_server` 也未传入该值 | **修复**：`update_config` 新增 `priority_reserve_ratio`（校验 [0,0.9]，非法保留原值），`_apply_config_changes` 传入；回归 `test_update_config_applies_priority_reserve_ratio`、`test_update_config_rejects_invalid_priority_reserve_ratio`、`test_inference_server_hot_reload_forwards_priority_reserve_ratio` |
| `/agent-status` body 非合法 UTF-8 时 `raw.decode` 抛 `UnicodeDecodeError`，`except` 仅捕 `JSONDecodeError` → 500 | **修复**：`except (JSONDecodeError, UnicodeDecodeError)` 统一返回 400（结构类客户端错误，非 5xx）；回归 `test_non_utf8_body_answers_400_not_500` |
| `count_pins_by_instance` 注释承诺 id≥`_COUNTER_INSTANCES` 回退全表扫描，实际只判 role，越界 id 两个方向都统计不到 | **修复**：每 role 计数区新增溢出标记，存在越界 id 的 pin 时 `count_pins_by_instance`/`record_balanced` 计数回退全表扫描（槽位布局升级 MSAFF7）；回归 `test_count_pins_by_instance_out_of_range_id_falls_back_to_scan` |
| `record_balanced` 收到 `None` `session_id`（仅带 parent 且父 pin 不存在时 `resolve_session_pin` 返回 fresh）时共享表 `_slot_session(None)` 抛 `AttributeError`，进程内表选实例却未落表 | **修复**：共享表与进程内表 `record_balanced` 对非字符串/空 session_id 提前返回 `None`，回退普通策略；回归 `test_record_balanced_without_session_id_returns_none`（两表） |
| `AgentSessionStateMachine._expire_locked` 每次访问全量遍历所有会话记录，POST/GET 路径 O(n) | **修复**：新增按 `updated_at_ms` 递增的过期索引 `_expiry_order`（apply 时增量维护），过期只从最旧端推进并在首个存活记录处停止（O(expired)）；回归 `test_expiry_is_incremental_and_does_not_scan_live_table`、`test_expiry_index_tracks_updates_and_lru_evictions` |
| `strict_priority` token 用 `token in set(tokens)` 普通比较，存在计时侧信道（前缀/匹配位置可探测） | **修复**：`_constant_time_token_match` 用 `hmac.compare_digest` 常数时间比较，并遍历全部候选 token 不做短路；回归 `test_strict_priority_token_uses_constant_time_compare`（断言候选数即比较次数）、`test_strict_priority_token_prefix_shared_values_match_exactly` |
| 默认（非 balanced）亲和路径构造 `eligible` 时未应用 `required_engine_type`/`required_dispatch_capability` 过滤，引擎类型切换后旧 pin 命中即 503 且不清理 | **修复**：抽出 `_affinity_eligible_instance_ids`（engine/dispatch 过滤 + 熔断排除），默认路径与 balanced 路径共用；不兼容 pin 解析为 `stale` 并回退普通策略；回归 `TestAffinityEligibleInstanceIds`（engine/dispatch/熔断/无过滤四例） |
| `record_balanced` 解包错位：`slot_retention` 实取第 10 项 `pinned_endpoint`，同 key 重绑会把 `retention_expires` 改成 endpoint id，抹掉 `cache_control` 承诺 | **修复**：统一按 `_read_slot` 顺序解包并抽出 `_prepare_slot_overwrite_locked`；仅同 key 重绑继承 retention；回归 `test_record_balanced_rebind_keeps_retention_not_endpoint`、`test_record_balanced_fresh_pin_has_zero_retention` |
| `replacing` 只看新 key（恒真），满表驱逐活条目时按新请求 role 给旧实例记数、并把被驱逐会话的 retention 原样写入新会话 | **修复**：按占据者区分「同 key 重绑 / 空槽·墓碑 / 驱逐活条目」——驱逐按占据者自身 role/instance 递减、retention 置 0；回归 `test_eviction_decrements_evicted_occupant_counter_across_roles`、`test_eviction_does_not_inherit_retention` |
| 驱逐候选只按 `last_used` 选择，声明期内的 `cache_control` pin 仍会被 LRU 驱逐 | **修复**：`_find_slot` 驱逐优先跳过 `retention_expires > now` 的槽，全部受保护时回退最旧条目；回归 `test_eviction_skips_retention_protected_pins`、`test_eviction_falls_back_when_all_pins_protected` |

## 5. 兼容性检查

- 默认配置（不设置任何新开关/env）下，基线调度行为不变：全部新逻辑由
  `MOTOR_SESSION_AFFINITY*` / `MOTOR_CACHE_CONTROL_*` / `agent_status_config.enabled`
  / `priority_reserve_ratio` / `priority_load_threshold` / `strict_priority_config.enabled`
  门控，缺省关闭；
- 既有客户端（不发送 `X-Agent-Hint`）零感知：body legacy 接口冻结，spec 字段不进入 body；
- `user_config.json` 增量兼容：新增字段均有默认值，既有配置文件无需修改——
  已用 `examples/deployer/config_tool/output_config/user_config.json` 实际加载并
  `validate_config()` 通过（全部新开关读出为默认关闭值）；
- 调度日志 `aff=` outcome 口径不变，新增字段仅追加。

## 6. 遗留项与边界

- 引擎侧 KV 动作（offload/prefetch，pause/suspend 钩子）与请求级 priority 引擎透传：
  依赖引擎消费能力，当前降级为状态跟踪（见设计 §4.6）；
- strict_priority 信任门为 bootstrap（静态 token），产品决策后平移至密钥表/mTLS，
  消费点无需改动；
- Prometheus 指标（hits/misses/stale/声明数）为后续横切项；
- 端到端实机联调（DSH/Pi/Hermes 三 harness 互操作）在开发仓完成过验证，本次合入
  以单元/集成测试与默认关闭的兼容性为验收基线。
