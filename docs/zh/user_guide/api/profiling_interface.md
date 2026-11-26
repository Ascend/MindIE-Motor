# 性能采集接口（Profiling）

本文档介绍由 Coordinator 统一触发性能采集（Profiling）的接口，适用于需要对指定服务、实例、DP 或 P/D 角色进行深度性能分析的用户。

## 接口说明

MindIE Motor 的推理引擎（vLLM Ascend）在启动时若配置了 `profiler-config`，会暴露 `/start_profile`、`/stop_profile` 接口。若逐个节点登录并调用引擎接口，无法按服务/实例/DP/P/D 角色统一选择目标，也无法统一记录触发结果。

Coordinator 的 Profiling 接口在 Observability 端口上提供统一入口：

1. 按 `service`（模型服务名，即实例 `model_name`）、`instances`（实例 ID）、`dps`（实例内 DP rank）、`roles`（P/D 角色）选择采集目标；
2. 从调度侧的实时实例视图解析出每个目标的具体引擎 endpoint；
3. 并行调用目标引擎的 `/start_profile` 或 `/stop_profile`，每批最多同时调用 8 台引擎；
4. 以与 metrics 一致的维度（`service` / `instance_id` / `role` / `dp_rank` / `pod_ip`）返回每个目标的采集结果，并为每个目标生成带维度标识的 `profile_prefix`。

采集链路：

```text
Client
  -> Coordinator Observability 端口 POST /profiling/start|stop
  -> 目标解析（service/instance/dp/role -> engine endpoint）
  -> 各目标引擎 POST /start_profile | /stop_profile
  -> 逐目标结果（含 metrics 维度标识 + profile_prefix）
```

>[!NOTE] 说明
>
> - Profiling 接口默认关闭，需在 `motor_coordinator_config.profiling_config.enable` 中显式开启，否则接口返回 HTTP `404`。
> - 主备部署时，仅主 Coordinator 接受 Profiling 请求；备 Coordinator 返回 HTTP `503`。
> - 开启 `mgmt_api_key_config.enable_api_key` 时，Profiling 接口与实例查询/刷新接口使用同一管理面 API Key（请求头 `X-Motor-Management-Key`）。
> - 性能数据文件由各引擎写入自身的 `profiler-config.torch_profiler_dir` 目录，Coordinator 不汇总、不转存采集文件。

## 采集前置配置

### 开启 Coordinator Profiling 接口

在 `user_config.json` 的 `motor_coordinator_config` 中增加：

```json
{
  "motor_coordinator_config": {
    "profiling_config": {
      "enable": true,
      "request_timeout_seconds": 10.0,
      "max_targets": 1024
    }
  }
}
```

参数说明参见 [profiling_config 字段](../configuration/config_reference.md#motor_coordinator_config)。

### 引擎开启 Profiler

在引擎配置（`motor_engine_prefill_config` / `motor_engine_decode_config` / `motor_engine_union_config` 的 `engine_config`）中增加 `profiler-config`：

```json
{
  "profiler-config": {
    "profiler": "torch",
    "torch_profiler_dir": "./vllm_profile",
    "torch_profiler_with_stack": false
  }
}
```

如果引擎未提供 Profiling 接口并返回 HTTP `404`，Coordinator 将该目标标记为 `failed`，在 `detail` 中返回错误，并继续处理其他目标。

## 接口格式

**请求类型**：`POST`

| 接口 | 方法 | 用途 |
|------|------|------|
| /profiling/start | POST | 对选中的目标启动 Profiling，返回逐目标结果 |
| /profiling/stop | POST | 停止匹配目标上当前的采集，返回逐目标结果 |

**IP/端口**：与 `GET /metrics` 相同，使用 Coordinator Observability 端口：

- 服务地址：`api_config.coordinator_api_host`，默认使用 Pod IP，未获取到时为 `127.0.0.1`。
- 端口：`api_config.coordinator_obs_port`，默认 `1027`；Kubernetes 部署时使用 `mindie-motor-coordinator-obs` 的 `nodePort`，默认 `31017`。
- 协议：`mgmt_tls_config.enable_tls=true` 时使用 `https`，否则使用 `http`。

### 目标选择参数

`POST /profiling/start` 与 `POST /profiling/stop`使用同一套目标选择参数，多个条件之间为 **AND** 关系：

| 参数名 | 类型 | 必选 | 说明 |
|--------|------|------|------|
| service | string | 否 | 模型服务名，对应实例 `model_name`。不传时匹配所有服务。 |
| roles | array[string] | 否 | P/D 角色筛选：`prefill` / `decode` / `union` / `encode`。筛选时 `both`、`hybrid` 与 `union` 等价，响应保留实例的原始角色标签。不传时匹配所有角色。 |
| instances | array[int] | 否 | 实例 ID 列表，即 metrics 中的 `instance_id`。不传时匹配服务/角色筛选后的全部实例。 |
| dps | array[int] \| object | 否 | DP rank 筛选，即 metrics 中的 `dp_rank`（`Endpoint.id`）。<br>数组形式：对每个选中实例应用同一组 DP rank；<br>对象形式：`{"实例ID": [DP rank, ...]}`，仅采集对象中列出的实例/DP 组合。 |
| profiler_options | object | 否 | 仅 `POST /profiling/start` 支持。透传给引擎 `/start_profile` 的 JSON 参数，例如 `profile_prefix`。Coordinator 会为该前缀补充目标维度后缀。 |

目标解析规则：

- 默认（不传任何选择参数）采集全部可用实例的全部可路由 DP；`headless` endpoint 不参与采集。
- `instances` 中出现未知实例 ID，或与 `service`/`roles` 筛选矛盾时返回 HTTP `400`。
- `dps` 数组形式要求每个选中实例都存在所列 DP rank，否则返回 HTTP `400`；对象形式中列出未知实例或未被选中的实例时返回 HTTP `400`。对象形式表示「只采集映射中列出的实例/DP 组合」，未在映射中列出的已选实例不参与采集。
- 解析出的目标数超过 `profiling_config.max_targets` 时返回 HTTP `400`，需缩窄选择范围。
- 去重前，`instances` 和 `dps` 数组各最多接受 4096 条。若 `dps` 为对象，实例键数和各列表的 DP rank 总数也各自最多为 4096。
- 没有任何目标匹配时返回 HTTP `400`。

### POST /profiling/start

**使用样例**

```bash
# 采集所有 Prefill 实例的全部 DP
curl -X POST "http://{IP}:{Port}/profiling/start" \
  -H "Content-Type: application/json" \
  -d '{"roles": ["prefill"]}'

# 采集实例 0 的 DP 0/1，以及实例 1 的 DP 2
curl -X POST "http://{IP}:{Port}/profiling/start" \
  -H "Content-Type: application/json" \
  -d '{"service": "deepseek_v3", "instances": [0, 1], "dps": {"0": [0, 1], "1": [2]}}'

# 采集全部实例的 DP 0，并指定采集文件前缀
curl -X POST "http://{IP}:{Port}/profiling/start" \
  -H "Content-Type: application/json" \
  -d '{"dps": [0], "profiler_options": {"profile_prefix": "manual_run"}}'
```

**响应示例**

```JSON
{
  "action": "start",
  "service": "deepseek_v3",
  "succeeded": 2,
  "failed": 0,
  "targets": [
    {
      "service": "deepseek_v3",
      "instance_id": 0,
      "role": "prefill",
      "dp_rank": 0,
      "pod_ip": "192.168.1.10",
      "address": "192.168.1.10:8000",
      "status": "started",
      "detail": "",
      "profile_prefix": "motor_profile_svc-deepseek_v3_ins-0_role-prefill_dp-0"
    },
    {
      "service": "deepseek_v3",
      "instance_id": 0,
      "role": "prefill",
      "dp_rank": 1,
      "pod_ip": "192.168.1.11",
      "address": "192.168.1.11:8000",
      "status": "started",
      "detail": "",
      "profile_prefix": "motor_profile_svc-deepseek_v3_ins-0_role-prefill_dp-1"
    }
  ]
}
```

**输出说明**

| 参数 | 类型 | 说明 |
|------|------|------|
| action | string | 本次操作：`start` / `stop`。 |
| service | string | 本次选择的服务名；跨服务选择时为空字符串。 |
| succeeded | integer | `start` 统计 `started`，`stop` 统计 `stopped`。 |
| failed | integer | 未成功收到引擎确认的目标数。 |
| targets[].service | string | 模型服务名，与 metrics 的 `model_name` 一致。 |
| targets[].instance_id | integer | 实例 ID，与 `GET /metrics?type=instance` 的 `instance_id` 标签一致。 |
| targets[].role | string | P/D 角色，与 metrics 的 `role` 标签一致。 |
| targets[].dp_rank | integer | DP rank，与 `GET /metrics?type=dp` 的 `dp_rank` 标签一致。 |
| targets[].pod_ip | string | DP 所在 Pod IP，与 `GET /metrics?type=dp` 的 `pod_ip` 标签一致。 |
| targets[].address | string | 引擎推理 endpoint（`host:port`）。 |
| targets[].status | string | 逐目标结果：`started` / `stopped` / `failed`。 |
| targets[].detail | string | 引擎调用失败的原因，过长时截断。成功时为空字符串。 |
| targets[].profile_prefix | string | 启动请求下发给引擎的采集前缀，停止请求返回空字符串。 |

### POST /profiling/stop

使用与启动相同的[目标选择参数](#目标选择参数)。停止请求按当时的可用拓扑解析目标，并停止匹配引擎上当前的采集。空对象 `{}` 匹配所有可用目标。

>[!NOTE] Coordinator 串行处理不同的 start/stop 请求。单次请求内最多同时调用 8 台引擎，任何一个调用结束后即可处理下一个目标。某台引擎失败会记录在结果里。若服务端任务被取消，Coordinator 等已下发的调用返回或超时后释放锁，跳过剩余目标。

```bash
# 停止当前所有 Prefill 目标的采集
curl -X POST "http://{IP}:{Port}/profiling/stop" \
  -H "Content-Type: application/json" \
  -d '{"roles":["prefill"]}'
```

响应结构与启动一致，`action` 为 `stop`。查看 `targets` 确认各目标的结果，可缩小选择范围后重试未停止成功的目标。

## 结果标识

Coordinator 为每个目标生成 `profile_prefix`，格式为：

```text
motor_profile_svc-<service>_ins-<instance_id>_role-<role>_dp-<dp_rank>
```

其中 `service` 中的非 `[0-9A-Za-z_.-]` 字符会替换为 `_`。该前缀与 metrics 维度一一对应，便于把各节点 `torch_profiler_dir` 下的采集文件映射回服务/实例/角色/DP：

- 自动生成的前缀会截短服务名，为实例、角色和 DP 后缀预留空间。调用方提供的前缀追加维度后缀后，必须为 1–128 个 ASCII 字符，以字母或数字开头，且只包含字母、数字、点、下划线或连字符。不符合时返回 HTTP `400`，不会向引擎下发 start。
- 引擎支持动态采集前缀时，会将该 `profile_prefix` 作为本次 trace 文件名前缀，采集文件可直接按维度检索；
- 引擎不支持动态前缀时，采集文件仍由引擎 `profiler-config` 决定，此时以响应中的 `targets[]` 维度与 `address`/`pod_ip` 作为文件归属依据。

>[!NOTE] 说明
>
> - Profiling 会引入显著的运行时开销，建议仅在定位性能问题时对必要的目标短时间开启。
> - 一次选择包含多个共用同一引擎地址的目标时返回 HTTP `400`，请缩小选择范围。

## 错误处理

| 场景 | HTTP 状态码 |
|------|-------------|
| Profiling 接口未开启（`profiling_config.enable=false`） | 404 |
| 请求体为空、JSON 非法、参数校验失败、目标无法解析 | `400` |
| 请求包含未知字段（如把 `roles` 拼成 `role`、`dps` 拼成 `dp`） | 400 |
| 请求体超过 1 MiB | 413，读取过程中超限即拒绝 |
| 管理面 API Key 缺失/错误（开启鉴权时） | 401/403 |
| 备 Coordinator 收到请求 | 503 |
| 调度侧实例视图不可用或无可用实例 | 503 |
| 引擎返回错误或调用超时 | 响应中该目标为 `failed`，原因见 `detail`。整批请求仍返回 HTTP `200`。 |

## 监控与后续操作

- 查看启动响应里的逐目标结果，再发送压测或业务请求触发有代表性的负载。
- 启动请求读取响应超时时，引擎可能已经开始采集。可用原选择器发 stop。
- 采集结束后调用 `POST /profiling/stop`；未收到停止命令的引擎可能继续采集。
- 采集文件位于各引擎 Pod 的 `profiler-config.torch_profiler_dir` 目录，可使用 MindStudio Insight 等工具分析。
