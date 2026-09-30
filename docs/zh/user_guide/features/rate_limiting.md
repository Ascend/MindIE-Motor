# 服务限流

## 特性介绍

服务限流在 Coordinator 推理面入口限制单位时间内进入的 HTTP 请求，避免瞬时流量把调度和推理实例打满。配置位于 `user_config.json` 的 `motor_coordinator_config.rate_limit_config`，默认关闭。

开启后，推理进程通过中间件处理进入推理面的请求。内置 `simple` 提供者使用全局令牌桶；`olc` 提供者使用过载控制库，按 URL、Method、IP 等标签匹配规则。请求体还可以单独设上限，超限返回 HTTP `413`，且不消耗限流令牌。

该能力只挂在 Coordinator 推理面应用上，覆盖例如：

- `POST /v1/completions`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `POST /v1/messages/count_tokens`
- `GET /v1/models`

管理面、Controller、Node Manager 和推理引擎进程不走这套限流。

### 工作原理

`simple` 使用一个全局令牌桶：

```text
桶容量     = max_requests
填充速率   = max_requests / window_size   （令牌/秒）
每个请求   = 消耗 1 个令牌
初始状态   = 满桶
```

令牌足够则放行，否则立即拒绝，不排队。`skip_paths` 中的路径按前缀匹配（`startswith`），命中后直接放行，不消耗令牌，也不做请求体大小检查。

推理面可以按 `inference_workers_config.num_workers` 启动多个 Worker 进程，默认 `4`。每个进程各自持有一只令牌桶，桶之间不共享，拥堵告警也按进程各自上报，Coordinator 不做去重。因此单个 Coordinator 上 `simple` 限流的总通过量大约是 `max_requests × num_workers`，不是配置里的单个 `max_requests`。多个 Worker 同时接近额度时，Controller 会收到多条拥堵事件。

`scope` 默认是 `global`。当前 `simple` 实现固定使用这一只全局桶，把 `scope` 改成其他值不会变成按 IP 或按用户限流。

限流检查失败时请求默认放行：`is_allowed` 抛错，或中间件调用限流器失败，都不会因为限流模块自身异常把推理面整段拒绝。

**请求拥堵事件**

`simple` 限流器在每次判定后，用已用额度 `used = max_requests - available`（容量减去当前剩余令牌）与 `max_requests` 的比例比较，并向 Controller 上报 `ReqCongestionEvent`：

| 条件 | 行为 |
|------|------|
| 尚未上报，且 `used >= int(max_requests × 0.85)` | 上报。Controller 接受后才置位；未接受则保持未上报 |
| 已经上报，且 `used < int(max_requests × 0.75)` | 再上报。Controller 接受后才清除；未接受则保持已上报 |

`ControllerApiClient.report_alarms` 失败时不抛异常，返回 `ok=false`（HTTP 非 200 或传输异常）。限流器忽略过这个返回值时，状态会提前置位，Controller 恢复后也不再补报。现在只有返回 `ok` 才翻转状态。未接受时不挡住本次请求，并由之后的请求重试，间隔约 1 秒，避免 Controller 不可达时每个请求都同步打一次上报。

事件固定为 `alarm_id=0xFC001005`、名称 `Coordinator Request Congestion Alarm`、级别 MAJOR、`reason_id=DEALING_WITH_CONGESTION`。触发和恢复使用同一个 `reason_id`。附加信息里的数字是已用额度。空载满桶时 `used` 很小，不会告警。状态在置位后不会重复上报，直到已用额度落到 75% 阈值之下。告警按推理 Worker 进程各报各的，与上一节的独立令牌桶一致。

`max_requests` 为 1、2、3、4、7、8 时，`int(max_requests × 0.85)` 与 `int(max_requests × 0.75)` 相等，触发和恢复之间没有滞回区间，已用额度在该整数附近来回时会反复上报。其中 `max_requests` 为 1 时，恢复条件是 `used < 0`，置位之后不会清除。`max_requests >= 10` 时两个整数阈值至少相差 1。

**请求体大小**

`max_request_body_size` 单位是 MB（1 MB = 1024×1024 字节），允许小数，例如 `0.5`。启动校验拒绝负数。运行时小于等于 0 表示不限制；大于 0 时，体大小检查发生在消耗令牌之前：

- 带有合法 `Content-Length`：按该头比较，超限直接 `413`，不预读正文。
- 没有合法 `Content-Length`（例如 chunked）：先读实际字节，超限返回 `413`；未超限则把已读正文重放给下游。

带 `Content-Length` 且超限时，响应体为：

```json
{
  "error": "request_body_too_large",
  "message": "Request body size (52428800 bytes) exceeds maximum"
}
```

无 `Content-Length` 且实际字节超限时，`message` 为 `Request body size exceeds maximum (<上限字节数> bytes)`。

**拒绝响应与响应头**

被限流拒绝的请求不会进入调度。默认 HTTP 状态码为 `error_status_code`（`429`），响应体为：

```json
{
  "error": "rate_limit_exceeded",
  "message": "too many requests, please try again later",
  "details": {
    "available": 0,
    "limit": 1000,
    "window_size": 60
  }
}
```

`simple` 在放行和拒绝的响应里都会带上：

```text
X-RateLimit-Remaining: <剩余令牌数>
X-RateLimit-Limit: <max_requests>
X-RateLimit-Window: <window_size>
```

**过载控制（provider: olc）**

`provider` 为 `olc` 且 `enable_rate_limit` 为 `true` 时，启动校验要求 `olc_config_path` 指向一个已存在的目录。目录内需有 OLC 规则文件（如 `overload-config.properties`、`olc.json`），启动校验只检查目录存在，不检查这些文件是否在目录中。文件缺失时，创建 OLC 中间件会失败并回退到令牌桶。Coordinator 将该路径写入环境变量 `OLC_CONFIG_PATH`，并挂载 `OlcFastAPIAdapter`。标签提取器提供：

| 标签 | 来源 |
|------|------|
| URL | 请求路径 |
| Method | HTTP 方法 |
| IP | 客户端地址；取不到时为 `unknown` |

配额、并发等规则由 OLC 配置决定，见仓库示例 `examples/features/http/overload_control` 与 [OLC 文档](https://gitcode.com/openFuyao/olc-python)。

OLC 中间件创建失败时，Coordinator 记录错误日志 `Using simple rate limit, Failed to create olc limit middleware`，并回退到内置令牌桶，服务仍能启动。

**热更新**

启动时已经创建 `simple` 限流器之后（`enable_rate_limit=true` 且 `provider` 为 `simple`，或 `olc` 创建失败后已回退），热更新会立即改这些字段：

- `skip_paths`、`error_message`、`error_status_code`、`max_request_body_size`
- `enable_rate_limit`：写入运行时开关。改为 `false` 后，后续请求直接放行，请求体大小检查也不再执行；改回 `true` 后继续走已有令牌桶
- `max_requests`、`window_size`：先按旧速率结算桶内令牌，再更新容量和填充速率。新容量更小则截断多余令牌；容量变大则把差额立即补进桶

配置重载要求 `max_requests` 和 `window_size` 都大于 0。等于 0 或负数不会进入令牌桶。令牌桶函数自身拒绝 `max_requests < 0` 和 `window_size <= 0`，并保持旧参数。

以下变更不能靠热更新完成，需要重启 Coordinator：

- 启动时 `enable_rate_limit=false`，之后想补装限流中间件
- 切换 `provider`，或修改 `olc_config_path`
- 修改 `scope`（当前实现也不读取该字段做分桶）

字段清单见 [热更新配置项说明](../configuration/update_config_whitelist.md)。

### 核心功能

- **全局限流**：`simple` 以令牌桶限制单个推理 Worker 进程在时间窗口内的请求数，超限立即返回，不排队。
- **请求体保护**：超过 `max_request_body_size` 的请求返回 HTTP `413`，且不消耗令牌。
- **路径豁免**：`skip_paths` 按前缀跳过限流和请求体检查，默认可放过探针、文档和 `/v1/metaserver`。
- **过载控制**：`olc` 按 URL、Method、IP 匹配外部规则；加载失败时回退到内置令牌桶。
- **拥堵告警**：已用额度达到容量的 85% 时向 Controller 上报一次，低于 75% 时再上报并清除状态。
- **热更新**：启动时已创建 `simple` 限流器后，可在不重启的情况下调整额度、豁免路径、错误响应和运行时开关。

### 约束与限制

| 维度 | 说明 |
|------|------|
| 部署场景 | <ul><li>PD 分离服务部署</li><li>PD 混部服务部署</li><li>Coordinator 独立部署</li></ul>只作用于 Coordinator 推理面 |
| 引擎 | 与推理引擎类型无关 |
| 特性互斥 | 无 |
| 软件依赖 | `provider=olc` 时需要安装 [OLC](https://gitcode.com/openFuyao/olc-python)（`olc-python` v0.1.0） |
| 其他限制 | <ul><li>`simple` 按每个推理 Worker 进程独立计数，总通过量约为 `max_requests × num_workers`</li><li>拥堵告警同样按 Worker 进程分别上报，不会在 Coordinator 内合并成一条</li><li>`max_requests` 为 1、2、3、4、7、8 时，85% 与 75% 取整后阈值相同，边界附近可能反复上报；为 1 时告警置位后不会清除</li><li>`scope` 不改变分桶方式，当前固定为进程内全局桶</li><li>启动时未开启限流时，热更新不能补装中间件</li><li>成功加载的 `olc` 不能通过热更新切换提供者或规则目录</li></ul> |

## 特性使用

### 使用场景

- 需要限制 Coordinator 推理入口的请求速率，避免突发流量打满调度和推理实例。
- 需要拒绝过大的请求体，避免超大 body 占用内存。
- 需要按 URL、方法或客户端地址使用外部过载规则，而不是单一全局额度。

### 使用样例

在 `motor_coordinator_config` 中增加 `rate_limit_config`。下面是内置令牌桶的最小配置，表示约每 60 秒 1000 个请求（平均约 16.7 QPS），每个推理 Worker 进程各算一份：

```json
{
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "provider": "simple",
      "max_requests": 1000,
      "window_size": 60
    }
  }
}
```

全量字段示例：

```json
{
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "provider": "simple",
      "max_requests": 1000,
      "window_size": 60,
      "scope": "global",
      "skip_paths": [
        "/liveness",
        "/readiness",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
        "/startup",
        "/v1/metaserver"
      ],
      "error_message": "too many requests, please try again later",
      "error_status_code": 429,
      "max_request_body_size": 0,
      "olc_config_path": ""
    }
  }
}
```

使用 OLC 时先安装库，再把 `olc_config_path` 指到规则目录：

```bash
pip install git+https://gitcode.com/openFuyao/olc-python.git@v0.1.0
```

```json
{
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "provider": "olc",
      "olc_config_path": "examples/features/http/overload_control/config"
    }
  }
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable_rate_limit | bool | `false` | 是否在启动时安装限流中间件 |
| provider | string | `simple` | `simple` 或 `olc`。`olc` 创建失败时回退 `simple` |
| max_requests | int | `1000` | 令牌桶容量，必须为正数。仅 `simple` 使用 |
| window_size | int | `60` | 时间窗口（秒），必须为正数。填充速率 = `max_requests / window_size` |
| scope | string | `global` | 当前 `simple` 固定全局桶，改该字段不改变分桶方式 |
| skip_paths | array | 见上例 | 不参与限流和请求体检查的路径前缀 |
| error_message | string | `too many requests, please try again later` | 拒绝文案 |
| error_status_code | int | `429` | 拒绝状态码，启动校验要求 `100`–`599` |
| max_request_body_size | float | `0` | 请求体上限（MB）。`0` 不限制；负数无法通过启动校验；运行时 `<= 0` 不检查 |
| olc_config_path | string | `""` | `provider=olc` 且开启限流时必填，且必须是已存在的目录 |

字段定义同时见 [配置参数说明](../configuration/config_reference.md#motor_coordinator_config) 的 **rate_limit_config字段**。Kubernetes PD 分离部署里的最小开关见 [服务限流](../deployment/k8s/pd_disaggregation_deployment.md#服务限流)。

关闭限流：删除 `rate_limit_config`，或设 `enable_rate_limit` 为 `false` 后重启。若启动时已经装上 `simple` 中间件，也可以热更新把 `enable_rate_limit` 改为 `false`。

### 验证特性

1. 按上面的 `simple` 配置重启 Coordinator。
2. 向推理面发送未命中 `skip_paths` 的请求。放行响应应带有 `X-RateLimit-Limit`、`X-RateLimit-Remaining` 和 `X-RateLimit-Window`。
3. 在一个 Worker 进程上持续打满 `max_requests`。超出后应返回 HTTP `429`，响应体 `error` 为 `rate_limit_exceeded`。
4. 将 `max_request_body_size` 设为较小正数后发送超限请求，应返回 HTTP `413`，且该请求不消耗令牌。
5. `/liveness`、`/readiness`、`/metrics` 等默认豁免路径应继续放行。

## 常见问题

### 改了配置但没有生效

**问题描述**

修改 `rate_limit_config` 后，未重启服务，限流行为没有变化。

**原因分析**

热更新只对启动时已经创建的 `simple` 限流器生效。启动时 `enable_rate_limit=false`，或一直使用已成功加载的 `olc` 时，运行中修改配置不会补装或切换中间件。

**解决步骤**

1. 确认启动配置里 `enable_rate_limit` 为 `true`，且当前提供者是 `simple`（或 `olc` 已回退为 `simple`）。
2. 若启动时未开启限流，或需要切换 `provider` / `olc_config_path`，重启 Coordinator。

### 实际通过量明显高于 max_requests

**问题描述**

已配置 `max_requests`，集群实际承受的请求量明显高于该值。

**原因分析**

`simple` 按每个推理 Worker 进程独立维护令牌桶。`inference_workers_config.num_workers` 大于 1 时，总通过量约等于 `max_requests × num_workers`。

**解决步骤**

按 Worker 进程数折算阈值，或调小 `max_requests`。

### 配置了按 IP 或按用户的 scope，仍然是全局限流

**问题描述**

`scope` 配成了 `per_ip` 或 `per_user`，限流仍按全局生效。

**原因分析**

当前 `simple` 不读取 `scope` 做分桶，始终是进程内全局令牌桶。

**解决步骤**

需要按 URL、方法或客户端地址限流时，改用 `provider: olc` 并配置对应规则。

### 日志出现 OLC 回退

**问题描述**

日志出现 `Using simple rate limit, Failed to create olc limit middleware`。

**原因分析**

OLC 未安装，或规则目录里的规则加载失败。Coordinator 已回退到内置令牌桶。

**解决步骤**

1. 安装 OLC：`pip install git+https://gitcode.com/openFuyao/olc-python.git@v0.1.0`。
2. 确认 `olc_config_path` 指向的目录存在，且包含 `overload-config.properties` 和 `olc.json`。
3. 重启 Coordinator。

### 启动报错 olc_config_path 或 error_status_code

**问题描述**

启动失败，报错含 `olc_config_path is required`、`does not exist`、`provider must be 'simple' or 'olc'`，或 `error_status_code must be in range 100-599`。

**原因分析**

`provider` 为 `olc` 且开启限流时，`olc_config_path` 必填且必须是已存在目录。`provider` 只能是 `simple` 或 `olc`。`error_status_code` 必须在 `100`–`599`。

**解决步骤**

按报错修正对应字段后重新启动。

### 客户端收到 429

**问题描述**

客户端收到 HTTP `429`，响应体 `error` 为 `rate_limit_exceeded`。

**原因分析**

当前进程的令牌已经用完。

**解决步骤**

查看 `details.available` 和响应头 `X-RateLimit-*`。调大 `max_requests`，或降低客户端请求速率。

### 客户端收到 413

**问题描述**

客户端收到 HTTP `413`，响应体 `error` 为 `request_body_too_large`。

**原因分析**

请求体超过 `max_request_body_size`（MB）。该拒绝不消耗令牌。默认 `0` 表示不限制。

**解决步骤**

调大 `max_request_body_size`，或缩小请求体。
