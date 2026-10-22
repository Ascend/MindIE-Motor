# Agent Hint 推理侧支持：会话亲和与调度控制链路

## 1. 背景与要解决的问题

Agent 框架通过 `X-Agent-Hint` HTTP 头（base64url JSON）向推理服务传递调度提示：
会话标识与派生关系（spawn/fork/handoff）、缓存保留声明（cache_control）、
上下文重写版本（epoch）、优先级（priority/strict_priority）、状态与预测
（agent_status/prediction）。

Coordinator 此前已有最小实现：请求体 `agent_hint` 块解析（session_control、
context_management 管理请求、块坐标回传引擎）与 `x-session-id` 头回退。
本设计在协议层与调度层完成 Agent Hint 的完整推理侧支持：

1. **协议层**：spec schema 对齐、header/body 通道边界、wire 层映射；
2. **调度层**：会话亲和（角色隔离、epoch 失效、父会话传播）、fresh 均衡放置、
   软优先级准入（限流预留 + 软负载门槛）、strict_priority 信任机制；
3. **会话生命周期**：agent_status 带外事件通道、finalize 释放亲和、
   cache_control 声明的保活与到期释放。

总原则：**畸形输入永不使请求失败**（WARN + 丢弃）；所有新行为默认关闭，
默认配置下基线调度零变化。

## 2. 协议边界（三条互不越界的通道）

| 通道 | 载体 | 职责 |
| --- | --- | --- |
| spec 带内 | `X-Agent-Hint` 头（base64url JSON） | spec 全部字段的唯一合法载体（嵌套 `session{}`、`agent_status`、`prediction`、`strict_priority`、spec `cache_control`） |
| legacy 带内 | 请求体 `agent_hint` 块 | 冻结接口：仅接受既有键（扁平 session_id、ephemeral cache_control{ttl,msg_offset}、context/session_control、latency/priority_control）；spec 键按未知字段进 raw_extra |
| 带外事件 | `POST/GET /agent-status`（推理面） | Agent 框架状态事件（running/tool/pause/suspend/finalize），只写会话状态机与亲和 pin 生命周期，不进入请求 body、不透传引擎 |

通道裁决规则：

- wire 层（`agent_hint_wire.py`）仅做 spec→legacy 映射（扁平 `session_id/parent_session_id`、
  `priority.level`→`priority_control.priority`、`prediction.latency_ms`→
  `latency_control.latency_sensitivity`，fill-if-absent，body wins），spec 块不进入 body——
  body 会原样转发引擎，未定义字段扩大引擎解析面，且 body 通道会绕过 strict_priority
  信任机制（安全边界）；
- session 标识解析优先级：body 扁平值 > header 嵌套 `session{}` > `x-session-id` 等头回退；
- 带内 `agent_status` 与带外状态机各自独立记录，不合并（避免双通道表达冲突）。

## 3. 模块与数据流

```text
请求 ──► fastapi_middleware（限流：优先级预留/strict 旁路）
     ──► inference_server（X-Agent-Hint 解码 → parse_agent_hint）
     ──► dispatch（strict_priority 信任裁决 → 请求信息）
     ──► AsyncSchedulerClient
            ├─ resolve_session_pin（亲和：own → parent，角色池内）
            ├─ balanced placement（fresh/stale/epoch 决策的负载均衡）
            ├─ _apply_priority_load_gate（软负载门槛，普通请求过滤）
            ├─ record(session_id, instance, role, epoch)
            └─ cache_declaration（显式 TTL 入账）+ keeper（保活/到期释放）
控制面：finalize 事件 ──► Mgmt dispatcher ──► PUB agent_status ──► 全体 worker drop pin
```

涉及模块：`domain/agent_hint.py`（Pydantic schema）、`domain/agent_hint_wire.py`、
`domain/agent_session_status.py`（状态机）、`domain/cache_declaration.py`（声明账本）、
`domain/block_offset_translator.py`（块坐标守卫）、`middleware/rate_limiter.py`、
`scheduler/runtime/{session_affinity,session_affinity_shared,scheduler_client,
scheduler_server,zmq_protocol}.py`、`api_server/inference_server.py`。

## 4. 子特性设计

### 4.1 Schema 对齐与 wire 映射

`agent_hint.py` 新增 Pydantic 模型：`SessionHint`（derivation/agent_type/call_index/
epoch）、`AgentStatusHint`（state 白名单 + tools）、`PredictionHint`（remaining_calls/
output_tokens/interval_ms/next_action 等）、`PriorityHint`（level/reason），
`AgentHintInfo` 结构化扩展，未知字段进 `raw_extra`。

spec `cache_control` 仅从 header 解析：`type: pin` 解析保留；`ttl_s` 与 legacy `ttl`
并存（`ttl_s` 优先，钳制 [60, 3600]）；`range.unit` **仅接受 `message`**——
`range.end` 映射为 `msg_offset`（1-based 生效起点，与 `compute_block_offset` 对齐），
`start>0` 与其它取值（含 `token`）丢弃 range + WARN、保留 `type`/`ttl` 等其余字段。
`token` 不是协议取值：只存储不换算会把"保留前 N token"悄悄放大为"保留整段会话"，
故收敛白名单并删除死代码分支；`compute_block_offset()` 对 `msg_offset=None` 增加守卫。

可观测性：`scheduled` 调度日志追加 `prio=<level>`、`aff=<outcome>`、`sp=<level>`、
`reason=` 字段。

### 4.2 会话亲和：角色隔离、epoch 失效、父会话传播

- **角色隔离**：亲和表键从 `session_id` 改为 `(role, session_id)`。P/D 分离下同一
  请求先分配 P 再分配 D，全局键会误删 P 的 pin 导致拓扑下粘性失效；角色池命名空间
  互不可见。
- **epoch 感知**：pin 记录创建时的 `session.epoch`。失效规则：仅当存储 epoch 与
  请求 epoch 均非 None 且不等时失效——兼容不带 epoch 的客户端（永不误杀）。
- **父会话传播**：子 agent 与父会话共享系统提示与长前缀，own pin 不存在时探测
  parent pin（不做 epoch 校验），避免重复 prefill。

pin 解析为纯函数 `resolve_session_pin(table, session_id, parent_session_id, epoch,
role, eligible) -> (instance_id, outcome)`，outcome ∈ `hit/parent/fresh/stale/epoch/none/-`
即 `aff=` 日志取值。解析仅作用于未受 `SchedulingConstraint` 钉扎的请求（约束钉扎
优先）；分配成功后 `record` 子会话自己的 pin。亲和表支持进程内实现与
`/dev/shm` 共享内存实现（`MOTOR_SESSION_AFFINITY_SHARED=1`，跨 worker/重启可见，
不可用时捕获 ImportError 降级回进程内表）。

### 4.3 fresh 均衡放置（balanced placement）

粘滞模式下 fresh 是唯一放置时机，但常规策略是无记忆的瞬时负载选择：并发 fresh
burst（~50ms 内到达）各自读到同一份 workload SHM 快照、互相不可见提交，平局仲裁
退化为非确定落点，钉扎把偏差锁死放大（实验：8 会话请求分布 71/29，P90 TTFT
1839ms vs 基线 1235ms）。

`MOTOR_SESSION_AFFINITY_PLACEMENT=balanced` 对将新建 pin 的决策
（outcome ∈ fresh/stale/epoch）按三元组字典序取最小实例：

```text
score(i) = ( 亲和表中钉扎到 i 的会话数,   # 粘滞承诺负载（决策时即提交，摊匀跨批次到达）
             SHM active_tokens 合计,      # 瞬时在途负载
             placement_seq % len(tied) )  # 进程本地单调计数，摊匀 50ms 级 burst
```

`hit`/`parent` 路径零改动（钉扎跟随是粘滞收益来源）；候选集与钉扎路径过滤对齐
（engine_type/dispatch_capability/熔断），选中实例不会在后续钉扎解析中失败；
eligible 为空回退常规策略路径。共享表新增 `count_pins_by_instance()` 槽位扫描。
日志在 affinity INFO 行追加 `placement=balanced`，`aff=` 口径不变。

### 4.4 软优先级准入（限流预留 + 调度门槛）

优先级识别仅从 header 提取（middleware 不读请求体）：`extract_priority_from_hint_headers()`。

- **限流预留**：`RateLimitConfig.priority_reserve_ratio`（[0, 0.9]，0=关闭）——
  桶容量按比例预留，普通请求消费下限为 reserve（满桶时 429 行为同今天），优先级 ≥
  `priority_admission_level` 的请求可穿透预留额度；热更新同步重算。
- **调度软负载门槛**：`SchedulerConfig.priority_load_threshold`（端点负载评分门槛，
  0=关闭）——普通请求过滤"全部端点评分 ≥ threshold"的实例；**门槛永不导致请求失败**，
  过滤后为空返回原列表并 WARNING。权威重选路径（CAS 冲突兜底）不做门槛过滤。
- `priority_admission_level` 单一配置源在 `SchedulerConfig`，限流与调度共用。

### 4.5 strict_priority 信任机制

`strict_priority`（硬优先级）存在伪造风险，消费以配置式信任门为前置：

```text
配置：strict_priority_config { enabled: bool = false, tokens: list[str] = [] }
请求：X-Strict-Priority-Token: <token>
规则：enabled && tokens 非空 && token 命中 → 可信；否则入口剥离（置 None + WARN）
```

信任规则唯一入口 `strict_priority_trusted()`，dispatch 与限流中间件共享；
不可信声明即不可见，不存在"未鉴权但被消费"的路径。`enabled=true` 而 tokens 为空
为配置校验错误。可信声明的消费点：限流**全局旁路**（不消耗令牌、永不 429）、
软负载门槛**完全豁免**。信任入口的唯一化使鉴权/签名/mTLS 升级不触碰消费点。
`priority.reason` 无信任要求，进 `reason=` 审计字段。

### 4.6 agent_status 带外事件通道与会话状态机

端点挂推理面 app（Agent 框架 baseURL 指向推理端口，origin 推导 `origin + /agent-status`）：

- `POST /agent-status`：结构性错误（非 JSON/缺 session_id/缺 agent_status）→ 400；
  字段级畸形 → 200 + `{"applied": false, "reason": ...}` + WARN（畸形永不失败，
  且客户端只认 2xx，400 会触发无意义重试）；不调用 `verify_api_key`，`/agent-status`
  加入 APIKey 与限流默认 skip 集；
- `GET /agent-status?session_id=`：查询状态记录（无记录 404）。

`AgentSessionStateMachine`（worker 进程内有界 LRU，默认 65536 条）：迁移规则
乱序全容忍、`finalize` 终态幂等（终态后其他状态 applied=false）、TTL 按服务端
接收时间惰性过期（不信任客户端时钟）、无后台清扫线程。pause/suspend 的引擎侧
KV 动作（offload/prefetch）依赖引擎消费能力确认，当前降级为状态跟踪 + WARN 钩子。

**finalize → 释放会话亲和 pin**（跨 worker）：

```text
worker（POST /agent-status, state=finalize）
  ├─ 本表直接 drop_all_roles(session_id)      # 单 worker 即时生效（主路径）
  └─ DEALER notify → Mgmt dispatcher
        └─ PUB topic agent_status → 所有 worker SUB
              └─ 各 worker drop_all_roles（幂等 no-op）
```

新增控制面请求类型 `AGENT_STATUS_NOTIFY` 与 PUB topic `agent_status`，镜像
circuit-breaker 事件的 fire-and-forget 模式（传输断连 WARN 丢弃）。PUB 不排队：
worker 未就绪时可能漏收，pin 残留由 epoch/stale/LRU 兜底；共享表模式下
`drop_all_roles` 单次本地清即全机生效，PUB 广播对回退表型（进程内表多 worker）
仍必要。默认 `enabled=false`：不注册路由、不建状态机、不发 notify。

### 4.7 cache_control 声明生命周期（affinity TTL）

会话亲和的绑定只受 LRU 容量、finalize、实例丢失约束，客户端"保留这段前缀 N 秒"
的声明此前无消费者。新增 `CacheDeclarationTable`（锁保护 OrderedDict 有界 LRU，
`time.monotonic` 基准，TTL 双重钳制）+ `AsyncSchedulerClient` 后台 keeper 任务。

**命名与边界**：本阶段交付物是 **affinity TTL**——保留声明的账本与亲和生命周期
（入账、保活、到期释放），**不是 KV 驻留保证**：keeper 只刷新路由表 LRU 与释放
pin，不改变引擎 block 的保留/驱逐策略；pin 存在时 KV 仍可能已被引擎驱逐，pin
删除后 KV 也可能仍驻留。真实的块级 retain/offload/prefetch 需要引擎能力协商与
完成反馈，属于 §4.6 预留钩子之后的后续能力（与 vLLM 上游 KvHintsEnvelope 的
"只传递、不执行"定位一致）。验收口径见验证文档 §1。

- **入账**：仅显式携带 `ttl`/`ttl_s` 的声明入账（解析层新增服务端派生字段
  `ttl_explicit` 区分"客户端要求了保留"与"解析层默认值 300"；不序列化进引擎
  payload）。三个主流 harness 均不发 `ttl_s`，该限定保证现网零行为影响；
- **到期释放**：sweep 取出到期声明后条件释放——pin 槽的 `renewed_at`（仅 record
  更新、get 触碰不动）晚于本 worker 声明的 wall 入账时间，说明其它 worker 在声明
  之后重新分配/续期了该 pin（其声明更晚、可能更长），此时跳过释放并把管理权交给
  续期 worker 的 keeper，避免本地短 TTL 账本误删共享表上仍有效的绑定；释放动作
  按 `_SWEEP_RELEASE_BATCH`（64/轮）限量，集中到期分多轮消化，不长时间阻塞事件
  循环；惰性过期不丢事件——请求路径与 keeper 并发时，过期声明移入
  pending-release 队列（有界）；
- **保活防护**：对活跃声明逐个 `get(session_id, role)` 刷新亲和 LRU recency，
  不改淘汰算法本身，代价离请求路径；
- **协同**：finalize/既有释放路径删声明并取消 pending release（keeper 不二次
  释放）；容量驱逐只丢账本不入队（溢出降级为今日行为，不做客户端未要求的
  破坏性动作）；keeper 单轮故障被吞掉并告警，绝不影响请求路径。

### 4.8 共享亲和表实现加固（评审修复）

`session_affinity_shared.py` 的共享内存布局升级（含后续回合，当前 `MSAFF7`），
修复多个评审发现的实现缺陷：

- **tombstone 删除**：线性探测表原先删除即清零槽位，截断探测链——与被删 key
  起始槽相同的后续 key 全部不可达（评审 194527742 两会话碰撞复现）。删除改为
  tombstone 标记（保留探测链，插入优先复用 tombstone 缩短链长）；
- **session 摘要标签**：槽位 session 区原存截断到 64 字节的原始 id，而 key 摘要
  按完整 id 计算——超长/多字节 id record 后 get/finalize 永远 miss（评审
  194527795 复现）。改为存 `sha256(session_id)[:32]` hex 标签，任意 id 全程一致；
- **O(1) per-instance 计数**：balanced 放置的钉扎计数原先在请求路径全表 flock
  扫描（65536 槽数十毫秒级）。文件头新增 per-(role, instance) 增量计数区
  （record/drop 同锁维护、attach 时全量对账修复漂移），`count_pins_by_instance`
  变为 O(1) 读取；未知 role、或存在 id≥`_COUNTER_INSTANCES` 的 pin（每 role 溢出
  标记非零）时回退全表扫描，避免越界实例漏统计（PR #1196 评审）。

### 4.9 agent_status 通道加固（评审修复）

- **跨 worker 终态**：PUB `agent_status` 扇出此前只驱动 pin 释放；worker 的
  状态机各自独立，A worker finalize 后 B worker 仍可接受 running，违反终态
  不可逆契约（评审 194528015）。现在扇出事件同时驱动各 worker 本地状态机
  （`agent_status_state_listener`，监听器故障只 WARN、不影响 pin 释放）；
  GET 仍只应答本进程记录（有界存储的设计限制不变）；
- **鉴权边界**：`agent_status_config.require_infer_auth`（默认 false，保持
  harness 无鉴权头契约）为 true 时，`/agent-status` 从 api-key 与限流的 skip
  集移除、继承推理面双门（评审 194528088）——finalize 可变更已知会话的亲和，
  推理端口超出可信网络时必须开启。

### 4.10 strict 信任门加固（评审修复）

- **热更新即时生效**：middleware 不再复制 enabled/tokens 快照，改为 live 读
  `RateLimitConfigHolder`，`_apply_config_changes` 同步刷新 holder 字段——
  禁用或轮转 token 后旧凭据立即失效（评审 194528148）；
- **凭据脱敏**：`X-Strict-Priority-Token` 加入 `filter_sensitive_headers`
  名单，DEBUG 头转储不再泄漏明文凭据（评审 194528212）；
- **数值容错**：hint 解析的全部数值入口统一 `_finite_int`（拒绝 `1e999` 类
  非有限数、捕获 OverflowError），嵌套 `prediction.next_action.tools[]` 的
  Pydantic 构造纳入异常边界——畸形可选 hint 保持 WARN + 丢弃，不使推理请求
  500（评审 194526933 / 194527501）。

## 5. 配置与开关

### 5.1 user_config 新增段（均有默认值，缺省=关闭=零行为变化）

| 段 | 字段 | 默认 | 说明 |
| --- | --- | --- | --- |
| `rate_limit_config.priority_reserve_ratio` | float | 0.0 | 限流预留比例 [0, 0.9] |
| `scheduler_config.priority_load_threshold` | int | 0 | 软负载评分门槛，0=关闭 |
| `scheduler_config.priority_admission_level` | int | 3 | 优先级 ≥ 该值豁免门槛/穿透预留 |
| `strict_priority_config.enabled` | bool | false | 信任门开关（开时 tokens 必须非空） |
| `strict_priority_config.tokens` | list[str] | [] | bootstrap 信任 token |
| `agent_status_config.enabled` | bool | false | `/agent-status` 端点 + 状态机（启动时生效） |
| `agent_status_config.session_ttl_s` | int | 3600 | 会话记录 TTL（>0） |
| `agent_status_config.max_sessions` | int | 65536 | 有界存储上限（>0） |
| `agent_status_config.require_infer_auth` | bool | false | true 时 `/agent-status` 继承推理面 API-key 与限流门 |

### 5.2 环境变量（跟随 session affinity 的 env 家族模式）

| 环境变量 | 含义 | 默认 |
| --- | --- | --- |
| `MOTOR_SESSION_AFFINITY` | 会话亲和总开关 | 关 |
| `MOTOR_SESSION_AFFINITY_SHARED` | 亲和表共享内存化（跨 worker/重启） | 关 |
| `MOTOR_SESSION_AFFINITY_PLACEMENT` | fresh 放置策略（`policy`/`balanced`） | policy |
| `MOTOR_CACHE_CONTROL_LIFECYCLE` | 声明生命周期总开关 | 关 |
| `MOTOR_CACHE_CONTROL_MAX` | 声明表容量 | 65536 |
| `MOTOR_CACHE_CONTROL_TTL_MAX_S` | TTL 上限钳制 | 3600 |
| `MOTOR_CACHE_CONTROL_SWEEP_INTERVAL_S` | keeper 周期（下限 1s） | 5 |

## 6. 失败模型与信任边界

- **解析层**：畸形 hint WARN + 丢弃，永不使请求失败（三条通道一致）；
- **调度层**：亲和/门槛/放置全部遵循"可用性优先"——eligible 为空回退常规路径；
- **信任边界**：`strict_priority` 配置式 token；`/agent-status` 无鉴权可写内容 =
  会话状态记录（有界）+ finalize 触发的 pin 清理（影响=缓存亲和扰动，与带内
  自报 hint 同级），恶意滥用不可能使请求失败；安全加固随后续鉴权能力对齐；
- **事件通道**：ZMQ PUB 不排队可能漏收，pin/状态残留由 TTL/LRU/stale 兜底；
- **资源边界**：状态机与声明表均 LRU 有界，pending-release 队列有界。

## 7. 测试与验证

单元与集成测试覆盖：协议解析（spec/legacy/畸形输入）、亲和角色隔离/epoch/parent/
LRU 边界、balanced 放置三元组与 burst 确定性、限流预留语义与热更新、门槛过滤与
兜底、信任门剥离、状态机迁移契约、finalize 跨进程释放、声明入账/到期释放/保活
协同。测试清单与执行记录见 [Agent Hint 验证记录](agent_hint_verification.md)。
