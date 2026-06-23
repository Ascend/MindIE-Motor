# user_config 全量参数说明

本文档对 `user_config.json` 中 Controller、Coordinator、NodeManager 等组件的**全量可配置项**进行说明，与 `examples/features/config_sample.json` 结构一一对应。部署时会将 `user_config.json` 中对应模块合并到组件运行时配置：先采用代码默认值，再按用户配置覆盖。支持在运行时通过修改组件所监控的配置文件实现动态生效。配置文件位于 `examples/infer_engines/` 目录下（如 `examples/infer_engines/vllm/user_config.json`），根据引擎类型和模型选择对应配置，以实际使用的为准。

## 1. motor_deploy_config（部署与资源）

`motor_deploy_config` 为部署与资源相关配置，由 `deploy.py` 读取并用于生成 K8s 资源、注入环境变量等。

**配置示例**：

```json
"motor_deploy_config": {
  "p_instances_num": 1,
  "d_instances_num": 1,
  "hybrid_instances_num": 1,
  "single_p_instance_pod_num": 1,
  "single_d_instance_pod_num": 1,
  "single_hybrid_instance_pod_num": 1,
  "p_pod_npu_num": 16,
  "d_pod_npu_num": 16,
  "hybrid_pod_npu_num": 2,
  "image_name": "",
  "job_id": "mindie-motor",
  "hardware_type": "800I_A3",
  "weight_mount_path": "/mnt/weight/",
  "deploy_mode": "infer_service_set",
  "tls_config": { ... }
}
```

| 配置项 | 类型 | 说明 |
|--------|------|------|
| p_instances_num | int | P 实例个数，≥1 且 ≤16 |
| d_instances_num | int | D 实例个数，≥1 且 ≤16 |
| hybrid_instances_num | int | PD 混部 union 实例个数，≥1 且 ≤16；PD 混部扩缩容时修改该字段 |
| single_p_instance_pod_num | int | 单个 P 实例对应的 Pod 数，≥1 |
| single_d_instance_pod_num | int | 单个 D 实例对应的 Pod 数，≥1 |
| single_hybrid_instance_pod_num | int | 单个 PD 混部 union 实例对应的 Pod 数，≥1 |
| p_pod_npu_num | int | 单个 P 实例 Pod 占用的 NPU 卡数，每个 Pod 最大 16 卡 |
| d_pod_npu_num | int | 单个 D 实例 Pod 占用的 NPU 卡数，每个 Pod 最大 16 卡 |
| hybrid_pod_npu_num | int | 单个 PD 混部 union Pod 占用的 NPU 卡数，每个 Pod 最大 16 卡 |
| image_name | string | 推理镜像名（需包含 MindIE-PyMotor 与 vLLM 等运行环境），与 [PD 分离服务部署](./pd_disaggregation_deployment.md#准备镜像) 中准备/加载的镜像名一致 |
| job_id | string | 部署任务名，同时作为 K8s 命名空间使用，如 `mindie-motor` |
| hardware_type | string | 硬件类型：<br>A2: 800I_A2<br>A3: 800I_A3<br>A5: 850-Atlas-8p-8|
| weight_mount_path | string | 宿主机上模型权重挂载路径，容器内 model_path 需与此挂载路径一致，如 `"/mnt/weight/"` |
| deploy_mode | string | 部署方式。可选：`infer_service_set`（默认，基于 InferServiceSet CRD，生成单个 infer_service.yaml 由 CRD controller 拉起各 pod）、`multi_deployment`（传统方式，生成 controller、coordinator、engine_*、kv_pool 等多个独立 YAML 分别 apply）、`single_container`（单容器方式，P/D 合并运行）。不配置时默认为 `infer_service_set`。CRD 方式尚未完成 RAS 能力与池化能力的适配验证；若需 RAS（可靠性、可用性、可服务性）或 KV 池化能力，请设置为 `multi_deployment` |
| tls_config | object | 可选；TLS 相关配置，含 infer_tls_config、mgmt_tls_config、etcd_tls_config、grpc_tls_config 四类，结构见 [PD 分离服务部署](./pd_disaggregation_deployment.md#tls_config可选) |

---

## 2. motor_controller_config

`motor_controller_config` 在 `examples/features/config_sample.json` 中的配置如下：

```json
"motor_controller_config": {
  "logging_config": {
    "log_level": "INFO",
    "log_max_line_length": 8192,
    "log_file": null,
    "log_format": "%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d]  %(message)s",
    "log_date_format": "%Y-%m-%d %H:%M:%S"
  },
  "api_config": {
    "controller_api_host": "127.0.0.1",
    "controller_api_port": 1026
  },
  "mgmt_tls_config": {
    "tls_enable": true,
    "ca_file": "security/mgmt/cert/ca.crt",
    "cert_file": "security/mgmt/cert/server.crt",
    "key_file": "security/mgmt/keys/server.key",
    "passwd_file": "security/mgmt/keys/key_pwd.txt",
    "crl_file": ""
  },
  "etcd_tls_config": { ... },
  "grpc_tls_config": { ... },
  "instance_config": { ... },
  "event_config": { ... },
  "fault_tolerance_config": { ... },
  "standby_config": { ... },
  "etcd_config": { ... }
}
```

### 2.1 logging_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| log_level | string | 日志级别。可选：`DEBUG`、`INFO`、`WARNING`、`ERROR` 等。默认：`INFO` |
| log_max_line_length | int | 单条日志最大长度，超过则截断。默认：`8192` |
| log_file | string/null | 日志输出文件路径；为 null 时输出到标准输出。默认：`null` |
| log_format | string | 日志格式模板，支持 Python logging 占位符。默认：`%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d]  %(message)s` |
| log_date_format | string | 日志日期格式，如 `%Y-%m-%d %H:%M:%S`。默认：`%Y-%m-%d %H:%M:%S` |

采用上述默认格式时，日志输出样例如下：

```txt
2026-02-12 14:30:00  [INFO][motor.coordinator][main.py:42]  Service started.
2026-02-12 14:30:01  [WARNING][motor.engine_server][service.py:128]  Retry connection to etcd.
2026-02-12 14:30:02  [ERROR][motor.controller][controller_api.py:56]  Request failed: connection timeout.
```

### 2.2 api_config

| 配置项                        | 类型 | 说明                                                              |
|----------------------------|------|-----------------------------------------------------------------|
| controller_api_host        | string | Controller API 监听地址（IP 或主机名）。默认：`127.0.0.1`（或 Env.pod_ip）       |
| controller_api_port        | int | Controller API 端口。默认：`1026`                                     |

### 2.3 mgmt_tls_config / etcd_tls_config / grpc_tls_config

三类 TLS 配置结构相同，字段如下。

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| tls_enable | bool | 是否开启 TLS。可选：`true` / `false`。默认：`true` |
| ca_file | string | CA 证书文件路径。默认：`security/mgmt/cert/ca.crt` |
| cert_file | string | 服务端证书文件路径。默认：`security/mgmt/cert/server.crt` |
| key_file | string | 私钥文件路径。默认：`security/mgmt/keys/server.key` |
| passwd_file | string | 私钥解密用密码文件路径。默认：`security/mgmt/keys/key_pwd.txt` |
| crl_file | string | 证书吊销列表（CRL）文件路径，可选，空串表示不使用。默认：`""` |

### 2.4 instance_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| instance_assemble_timeout | int | 等待实例就绪的最长等待时间（秒）。默认：`600` |
| instance_assembler_check_internal | int | 轮询实例组装状态的间隔（秒）。默认：`1` |
| instance_assembler_cmd_send_internal | int | 向实例下发组装命令的间隔（秒）。默认：`1` |
| instance_manager_check_internal | int | 实例状态巡检间隔（秒）。默认：`1` |
| instance_heartbeat_timeout | int | 超过该时长未收到实例心跳则判定异常（秒）。默认：`10` |
| instance_expired_timeout | int | 实例空闲超过该时长则被清理（秒）。默认：`300` |
| send_cmd_retry_times | int | 向实例下发命令失败时的重试次数。默认：`3` |

### 2.5 event_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| event_consumer_sleep_interval | float | 事件队列轮询间隔，即每次处理事件后的等待时间（秒）。默认：`1.0` |
| coordinator_heartbeat_interval | float | Controller 与 Coordinator 间心跳上报间隔（秒）。默认：`10.0` |

### 2.6 fault_tolerance_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| enable_fault_tolerance | bool | 是否启用故障自愈（高级 RAS）。可选：`true` / `false`。默认：`false` |
| strategy_center_check_internal | int | 策略中心轮询间隔（秒）。默认：`1` |
| enable_scale_p2d | bool | 是否启用 P2D 弹性扩缩容。可选：`true` / `false`。默认：`false` |
| enable_token_reinference | bool | 是否启用Token Reinference 故障恢复。可选：`true` / `false`。默认：`false` |

### 2.7 standby_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| enable_master_standby | bool | 是否开启 Controller 主备。可选：`true` / `false`。默认：`false` |
| master_standby_check_interval | int | 主备角色探测间隔（秒）。默认：`5` |
| master_lock_ttl | int | 主节点在 etcd 上占锁的租约时长（秒）。默认：`10` |
| master_lock_retry_interval | int | 抢主时获取锁的重试间隔（秒）。默认：`5` |
| master_lock_max_failures | int | 连续抢主失败超过此次数则放弃并切换。默认：`3` |
| master_lock_key | string | 主节点在 etcd 中的锁路径；运行时会自动加前缀 `/controller/`。默认：`/master_lock`（实际为 `/controller/master_lock`） |

### 2.8 etcd_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| etcd_host | string | etcd 服务地址（主机名或 IP）。默认：`etcd.default.svc.cluster.local` |
| etcd_port | int | etcd 端口。默认：`2379` |
| etcd_timeout | int | etcd 操作超时时间（秒）。默认：`5` |
| etcd_ca_cert | string/null | etcd CA 证书路径，可选；null 表示不使用。默认：`null` |
| etcd_cert_key | string/null | etcd 客户端私钥路径，可选。默认：`null` |
| etcd_cert_cert | string/null | etcd 客户端证书路径，可选。默认：`null` |
| enable_etcd_persistence | bool | 是否启用 etcd 持久化。可选：`true` / `false`。默认：`false` |

---

## 3. motor_coordinator_config

`motor_coordinator_config` 在 `examples/features/config_sample.json` 中的配置如下：

```json
"motor_coordinator_config": {
  "logging_config": {
    "log_level": "INFO",
    "log_max_line_length": 8192,
    "log_file": null,
    "log_format": "%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d]  %(message)s",
    "log_date_format": "%Y-%m-%d %H:%M:%S"
  },
  "prometheus_metrics_config": {
    "reuse_time": 3
  },
  "exception_config": {
    "max_retry": 5,
    "retry_delay": 0.2,
    "first_token_timeout": 600,
    "infer_timeout": 3600
  },
  "scheduler_config": {
    "scheduler_type": "load_balance"
  },
  "infer_tls_config": { ... },
  "mgmt_tls_config": { ... },
  "etcd_tls_config": { ... },
  "timeout_config": { ... },
  "api_key_config": { ... },
  "rate_limit_config": { ... },
  "standby_config": { ... },
  "etcd_config": { ... },
  "api_config": { ... },
  "aigw_model": null
}
```

### 3.1 logging_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| log_level | string | 日志级别。可选：`DEBUG`、`INFO`、`WARNING`、`ERROR` 等。默认：`INFO` |
| log_max_line_length | int | 单行日志最大长度，超过则截断。默认：`8192` |
| log_file | string/null | 日志文件路径；null 时输出到标准输出。默认：`null` |
| log_format | string | 日志格式模板，支持 Python logging 占位符。默认：`%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d]  %(message)s` |
| log_date_format | string | 日志日期格式。默认：`%Y-%m-%d %H:%M:%S` |

### 3.2 prometheus_metrics_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| reuse_time | int | Prometheus 指标缓存复用时长（秒）。默认：`3` |

### 3.3 exception_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| max_retry | int | 请求失败后的最大重试次数。默认：`5` |
| reschedule_enabled | bool | 是否缓存流式响应 token ID，以便瞬时传输故障后重调度并续接请求。该配置不控制引擎侧 recompute。默认：`true` |
| transport_max_retry | int/null | Coordinator 传输失败的最大尝试次数；`null` 时使用 `max_retry`。默认：`null` |
| retry_delay | float | 每次重试前的等待时间（秒）。默认：`0.2` |
| first_token_timeout | int | 等待首 token 返回的超时时间（秒）。默认：`600` |
| infer_timeout | int | 单次推理请求的总超时时间（秒）。默认：`3600` |
| upstream_error_body_max_bytes | int | 向客户端透传引擎 HTTP 错误体的最大字节数，避免返回超大错误响应。默认：`65536` |

> `recompute_enabled` 仅作为 `reschedule_enabled` 的旧配置兼容别名；`recompute_max_retry` 已移除并会被忽略。模型重计算由引擎侧负责。

流式请求会在上游接受请求后再提交 HTTP 200；Unified PD 模式需等待 Prefill 和 Decode 两路均接受请求。
提交前的引擎错误会保留原 HTTP 状态码、受限大小的响应体以及安全响应头；提交后 HTTP 状态码已不可修改，
引擎 JSON 错误体会作为 SSE `data` 事件返回。

### 3.4 scheduler_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| scheduler_type | string | 调度类型。<ul><li>load_balance：负载均衡；</li><li>round_robin：轮询；</li><li>kv_cache_affinity：KV Cache 亲和调度。</li></ul>默认：load_balance |
| endpoint_instance_score_weight | float | endpoint 优先负载均衡时实例平均负载权重。默认：`0.05` |
| kv_affinity_mode | string | `scheduler_type=kv_cache_affinity` 时的子策略：`unified`（默认）或 `load_gated` |
| kv_affinity_load_weight | float | unified 模式下 endpoint 实时负载权重。默认：`1.0` |
| kv_affinity_overlap_credit | float | 缓存前缀对 prefill 成本的折扣系数。默认：`1.0` |
| kv_affinity_prefill_load_scale | float | unified 模式下（经亲和折扣后的）prefill 成本权重。默认：`1.0` |
| kv_affinity_load_gate_topn | int | load_gated 模式下先保留负载最低的 N 个 endpoint 再做亲和择优；`0` 时回退为 `2`。默认：`0` |

**prefill_kv_event_config 自动推导**（加载 `user_config.json` 时由 Coordinator 合并，一般无需手写）：

| 来源 | 说明 |
|------|------|
| PD 分离 | 从 `motor_engine_prefill_config.engine_config.kv-events-config` 推导 |
| PD 混部 | 从 `motor_engine_union_config.engine_config.kv-events-config` 推导 |
| kv_conductor_config | `http_server_port` 写入 `prefill_kv_event_config.http_server_port`；未配置时默认 `13333` |

Coordinator 会根据实例角色自动识别 P/D 分离或 union 混部拓扑，并根据引擎 Connector 推导、由 NodeManager 内部上报的 `dispatch_capabilities` 选择并发或 handoff 行为。该字段不支持用户显式配置；自定义 Connector 可在 `motor_engine_prefill_config` / `motor_engine_decode_config` 顶层使用 `dispatch_profile` 声明语义（见 [§6.1 dispatch_profile](./config_reference.md#61-dispatch_profilepd-协同语义)）。Connector 识别白名单、`MultiConnector` 取 `connectors[0]` 的规则，以及未识别连接器导致路由 503（fail-closed）的处理，详见 [PD 分离特性说明](../../../design/pd_disaggregation.md#vllm-connector-识别白名单) 与 [PD 分离服务部署](./pd_disaggregation_deployment.md#kv_connector-识别白名单与-dispatch_profile)。

### 3.5 infer_tls_config / mgmt_tls_config / etcd_tls_config

三类 TLS 配置结构相同，字段如下。

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| tls_enable | bool | 是否开启 TLS。可选：`true` / `false`。默认：`true` |
| ca_file | string | CA 证书文件路径。默认：`security/mgmt/cert/ca.crt` |
| cert_file | string | 服务端证书文件路径。默认：`security/mgmt/cert/server.crt` |
| key_file | string | 私钥文件路径。默认：`security/mgmt/keys/server.key` |
| passwd_file | string | 私钥解密用密码文件路径。默认：`security/mgmt/keys/key_pwd.txt` |
| crl_file | string | 证书吊销列表（CRL）文件路径，可选，空串表示不使用。默认：`""` |

### 3.6 timeout_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| request_timeout | int | 单次 HTTP 请求超时时间（秒）。默认：`30` |
| connection_timeout | int | 建立连接的超时时间（秒）。默认：`10` |
| read_timeout | int | 读操作超时时间（秒）。默认：`15` |
| write_timeout | int | 写操作超时时间（秒）。默认：`15` |
| keep_alive_timeout | int | 连接保活时长，超时无活动则关闭（秒）。默认：`60` |

### 3.7 api_key_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| enable_api_key | bool | 是否开启 API Key 鉴权。可选：`true` / `false`。默认：`false` |
| valid_keys | array | 合法的 API Key 字符串列表。默认：`[]` |
| encryption_algorithm | string | Key 校验使用的加密算法，如 `PBKDF2_SHA256`。默认：`PBKDF2_SHA256` |
| header_name | string | 携带 API Key 的 HTTP 头名称。默认：`Authorization` |
| key_prefix | string | 头中 Key 的前缀，如`Bearer`。默认：`Bearer`|
| skip_paths | array | 不校验 API Key 的路径列表（如 `/metrics`、`/liveness`、`/docs` 等），可自定义 |

### 3.8 rate_limit_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| enable_rate_limit | bool | 是否开启请求限流。可选：`true` / `false`。默认：`false` |
| max_requests | int | 限流时间窗口内允许的最大请求数。默认：`1000` |
| window_size | int | 限流统计的时间窗口长度（秒）。默认：`60` |
| scope | string | 限流生效范围，如 `global`（全局）。默认：`global` |
| skip_paths | array | 不参与限流统计的路径列表（如 `/liveness`、`/readiness`、`/metrics`），可自定义 |
| error_message | string | 触发限流时返回给客户端的提示文案。默认：`too many requests, please try again later` |
| error_status_code | int | 触发限流时返回的 HTTP 状态码，通常为 4xx（如 429）。默认：`429` |

### 3.9 standby_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| enable_master_standby | bool | 是否开启 Coordinator 主备。可选：`true` / `false`。默认：`false` |
| master_standby_check_interval | int | 主备角色探测间隔（秒）。默认：`5` |
| master_lock_ttl | int | 主节点在 etcd 上占锁的租约时长（秒）。默认：`10` |
| master_lock_retry_interval | int | 抢主时获取锁的重试间隔（秒）。默认：`5` |
| master_lock_max_failures | int | 连续抢主失败超过此次数则放弃并切换。默认：`3` |
| master_lock_key | string | 主节点在 etcd 中的锁路径；运行时会自动加前缀 `/coordinator/`。默认：`/master_lock`（实际为 `/coordinator/master_lock`） |

### 3.10 etcd_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| etcd_host | string | etcd 服务地址（主机名或 IP）。默认：`etcd.default.svc.cluster.local` |
| etcd_port | int | etcd 端口。默认：`2379` |
| etcd_timeout | int | etcd 操作超时时间（秒）。默认：`5` |
| enable_etcd_persistence | bool | 是否启用 etcd 持久化。可选：`true` / `false`。默认：`false` |
| tls_config | object | etcd 客户端 TLS，可选。子字段：enable_tls（true/false）、ca_cert、tls_cert、tls_key、tls_passwd |

### 3.11 api_config

| 配置项                        | 类型 | 说明                                                         |
|----------------------------|------|------------------------------------------------------------|
| coordinator_api_host       | string | Coordinator API 监听地址（IP 或主机名）。默认：`127.0.0.1`（或 Env.pod_ip） |
| coordinator_api_dns        | string | 域名                                                         |
| coordinator_api_infer_port | int | 推理面端口。默认：`1025`                                            |
| coordinator_api_mgmt_port  | int | 管控面端口。默认：`1026`                                            |
| coordinator_obs_port       | int | Observability 端口，承载 `/metrics` 等可观测性接口。默认：`1027`      |

### 3.12 request_limit（user_config 常用）

`config_sample.json` 中未包含此块，但 PD 部署时常用；合并到运行时配置后生效。

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| single_node_max_requests | int | 单节点允许的最大并发请求数，由 user_config 配置 |
| max_requests | int | 集群全局最大并发请求数，由 user_config 配置 |

### 3.13 aigw_model

`aigw_model` 是 AIGW 模型元数据的集中配置，用于 `/v1/models` 等接口返回的模型信息。在 `user_config.json` 中对应 `motor_coordinator_config` 下的 **`aigw`** 对象；未使用时为 `null`。其内部可配置项如下。

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| id | string | 模型 ID，与 OpenAI 兼容接口中的模型名一致。若配置了 Prefill/Decode 的 model_name，部署时会自动填充为 Prefill 的 model_name |
| object | string | 对象类型，固定为 `model`。部署时未配置则自动填充 |
| owned_by | string | 模型归属标识，如 `motor`。部署时未配置则自动填充 |
| p_max_seqlen | int | Prefill 端最大序列长度（正整数）。未配置时从 Prefill 的 `engine_config.max_model_len` 自动填充 |
| d_max_seqlen | int | Decode 端最大序列长度（正整数）。未配置时从 Decode 的 `engine_config.max_model_len` 自动填充 |
| slo_ttft | int | 首 token 时延 SLO（毫秒），用于调度/监控。默认：`1000` |
| slo_tpot | int | 每 token 时延 SLO（毫秒），用于调度/监控。默认：`50` |

---

## 4. motor_nodemanger_config

`motor_nodemanger_config` 在 `examples/features/config_sample.json` 中的配置如下：

```json
"motor_nodemanger_config": {
  "api_config": {
    "pod_ip": "127.0.0.1",
    "host_ip": "127.0.0.1",
    "node_manager_port": 1026,
    "controller_api_dns": "127.0.0.1",
    "controller_api_port": 1026
  },
  "mgmt_tls_config": {
    "tls_enable": true,
    "ca_file": "security/mgmt/cert/ca.crt",
    "cert_file": "security/mgmt/cert/server.crt",
    "key_file": "security/mgmt/keys/server.key",
    "passwd_file": "security/mgmt/keys/key_pwd.txt",
    "crl_file": ""
  },
  "endpoint_config": {
    "endpoint_num": 0,
    "base_port": 10000,
    "mgmt_ports": [],
    "service_ports": []
  },
  "basic_config": {
    "job_name": null,
    "role": "both",
    "model_name": "",
    "hardware_type": "800I-A3",
    "heartbeat_interval_seconds": 3,
    "device_num": 0,
    "parallel_config": {
      "dp_size": 1,
      "cp_size": 1,
      "tp_size": 1,
      "sp_size": 1,
      "ep_size": 1,
      "pp_size": 1,
      "world_size": 1
    }
  },
  "logging_config": {
    "log_level": "INFO",
    "log_max_line_length": 8192,
    "log_file": null,
    "log_format": "%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d]  %(message)s",
    "log_date_format": "%Y-%m-%d %H:%M:%S"
  }
}
```

### 4.1 api_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| pod_ip | string | Pod IP（由环境或部署注入）。默认：`127.0.0.1`（或 Env.pod_ip） |
| node_manager_port | int | NodeManager 端口。默认：`1026` |
| controller_api_dns | string | Controller API 域名或 IP，多由部署或环境注入。默认：`127.0.0.1` |
| controller_api_port | int | Controller API 端口。默认：`1026` |

### 4.2 mgmt_tls_config

与 2.3 中 TLS 配置结构相同，字段如下。

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| tls_enable | bool | 是否开启 TLS。可选：`true` / `false`。默认：`true` |
| ca_file | string | CA 证书文件路径。默认：`security/mgmt/cert/ca.crt` |
| cert_file | string | 服务端证书文件路径。默认：`security/mgmt/cert/server.crt` |
| key_file | string | 私钥文件路径。默认：`security/mgmt/keys/server.key` |
| passwd_file | string | 私钥解密用密码文件路径。默认：`security/mgmt/keys/key_pwd.txt` |
| crl_file | string | 证书吊销列表（CRL）文件路径，可选，空串表示不使用。默认：`""` |

### 4.3 endpoint_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| endpoint_num | int | 引擎端点数量，通常由 HCCL/并行配置推导。默认：`0` |
| base_port | int | 端点端口起始号。默认：`10000` |
| mgmt_ports | array | 各端点管控端口列表（整数数组）。默认：`[]` |
| service_ports | array | 各端点推理服务端口列表（整数数组）。默认：`[]` |

### 4.4 basic_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| job_name | string/null | 任务/作业名，多由环境或 deploy 注入。默认：Env.job_name 或 null |
| role | string | 本节点角色。可选：`prefill`（仅预填）、`decode`（仅解码）、`both`（预填+解码）。默认：`both` |
| model_name | string | 模型名称，PD 部署时多由 user_config 注入。默认：`""` |
| hardware_type | string | 硬件类型：<br>A2: 800I_A2<br>A3: 800I_A3<br>A5: 850-Atlas-8p-8 |
| heartbeat_interval_seconds | int | 向 Controller 上报心跳的间隔（秒）。默认：`3` |
| device_num | int | NPU 设备数量，多由 HCCL 配置推导。默认：`0` |
| parallel_config | object | 并行维度配置，见下表。默认：各维度 1，world_size 由系统根据各维度自动计算 |

**parallel_config 子字段**：

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| dp_size | int | 数据并行度。默认：`1` |
| cp_size | int | 上下文并行度。默认：`1` |
| tp_size | int | 张量并行度。默认：`1` |
| sp_size | int | 序列并行度。默认：`1` |
| ep_size | int | 专家并行度。默认：`1` |
| pp_size | int | 流水并行度。默认：`1` |
| world_size | int | 总进程数；为 0 时由系统按 dp×cp×tp×pp 自动计算。默认：`0` |

### 4.5 logging_config

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| log_level | string | 日志级别。可选：`DEBUG`、`INFO`、`WARNING`、`ERROR` 等。默认：`INFO` |
| log_max_line_length | int | 单条日志最大长度，超过则截断。默认：`8192` |
| log_file | string/null | 日志输出文件路径；为 null 时输出到标准输出。默认：`null` |
| log_format | string | 日志格式模板，支持 Python logging 占位符。默认：`%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d]  %(message)s` |
| log_date_format | string | 日志日期格式。默认：`%Y-%m-%d %H:%M:%S` |

---

## 5. motor_engine_union_config（PD 混部引擎）

`motor_engine_union_config` 用于 PD 混部场景，配置同一类 union Engine Server 实例。其结构与 `motor_engine_prefill_config` / `motor_engine_decode_config` 类似，但不区分 P/D 两套引擎配置，也无需配置 `kv_transfer_config` 的 producer/consumer 角色。示例可参考 `examples/infer_engines/vllm/pd_hybrid/user_config.json`。

**配置示例**：

```json
"motor_engine_union_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "gpu_memory_utilization": 0.9,
    "data_parallel_size": 1,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "enable_expert_parallel": false,
    "data_parallel_rpc_port": 9000,
    "enforce-eager": true,
    "max_model_len": 2048
  }
}
```

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| engine_type | string | 引擎类型，如 `vllm` |
| engine_config | object | 引擎相关配置，含模型信息、并行策略和引擎原生参数 |
| engine_config.served_model_name | string | 对外服务的模型名称 |
| engine_config.model | string | 容器内模型权重路径，需与 `motor_deploy_config.weight_mount_path` 挂载后一致 |
| engine_config.gpu_memory_utilization | float | NPU 内存使用占比上限，0～1 |
| engine_config.data_parallel_size | int | 数据并行大小 |
| engine_config.tensor_parallel_size | int | 张量并行大小 |
| engine_config.pipeline_parallel_size | int | 流水并行大小 |
| engine_config.enable_expert_parallel | bool | 是否启用 EP |
| engine_config.data_parallel_rpc_port | int | DP 侧 RPC 端口 |
| engine_config.max_model_len | int | 最大模型上下文长度 |
| 其它键 | - | 引擎原生参数，按所选用引擎文档直接填写 |

---

## 6. motor_engine_prefill_config / motor_engine_decode_config（P/D 引擎）

P/D 分离部署时，`motor_engine_prefill_config` 与 `motor_engine_decode_config` 分别配置 Prefill 与 Decode 引擎。两者结构相同，均需指定 `engine_type` 与 `engine_config`；可选配置 `dispatch_profile`（P/D 协同语义）与 `health_check_config`（虚推健康探测）。

**配置示例**：

```json
"motor_engine_prefill_config": {
  "engine_type": "sglang",
  "engine_config": { ... },
  "health_check_config": {
    "enable_virtual_inference": true,
    "npu_usage_threshold": 3,
    "max_failure_count": 6,
    "health_collector_timeout": 2
  }
}
```

PD 模式下 P 与 D **各自独立配置** `health_check_config`；未配置时使用代码默认值。引擎 `engine_config` 字段说明见 [PD 分离服务部署](./pd_disaggregation_deployment.md#motor_engine_prefill_config--motor_engine_decode_configpd-引擎)。

### 6.1 dispatch_profile（P/D 协同语义）

当 `engine_config.kv_transfer_config.kv_connector` 不在内置识别白名单内时，可在 `motor_engine_prefill_config` / `motor_engine_decode_config` **顶层**显式声明 P/D 协同语义。NodeManager 据此推导并向 Coordinator 上报 `dispatch_capabilities`。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| dispatch_profile | string | 未配置（由 `kv_connector` 白名单推断） | P/D 协同语义。可选：`handoff`（Prefill 完成后交给 Decode）、`trigger`（P/D 并发，引擎同步 KV）。Prefill 与 Decode **两端须配置相同取值** |

| `dispatch_profile` | 推导出的 capability |
|--------------------|---------------------|
| `handoff` | `prefill_handoff_decode` |
| `trigger` | `concurrent_engine_sync` |

vLLM 内置识别的 `kv_connector` 白名单见 [PD 分离特性说明](../../../design/pd_disaggregation.md#vllm-connector-识别白名单)。白名单内 connector 一般**无需**手动配置 `dispatch_profile`。

**注意**：

- `dispatch_profile` 写在 `motor_engine_*_config` 顶层，**不是** `engine_config` 内部。
- 不支持用户直接填写 `dispatch_capabilities`，配置后会被 NodeManager 丢弃。
- 取值须与 connector 实际协同语义一致；P/D 不一致或无共同 capability 时，Coordinator 路由返回 503。

**配置示例**（自定义 connector）：

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_producer"
    }
  }
},
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_consumer"
    }
  }
}
```

### 6.2 health_check_config（虚推健康探测）

虚推由 Engine Server 周期性向推理面发送轻量请求，结合 NPU AICore 使用率判断引擎是否健康。机制说明见 [Engine Server 组件文档](../../../developer_guide/components/engine_server.md#虚推虚拟推理健康探测)。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable_virtual_inference | bool | `false` | 虚推总开关。为 `true` 时，在推理面 `/health` 正常后启动周期性虚推 |
| npu_usage_threshold | int | `3` | AICore 使用率阈值（%）。虚推仅在 `0 < npu_usage_threshold <= 100` 时启动；低于该阈值且虚推失败时累计失败次数 |
| max_failure_count | int | `6` | 连续虚推失败次数上限（在累计条件满足后），达到后 Engine Server `/status` 返回 abnormal |
| health_collector_timeout | int | `2` | 推理面 `GET /health` 探测超时（秒）；虚推启动的前置条件 |

---

## 7. env.json 补充说明（PD 混部）

PD 混部场景下，union Engine Server 的环境变量配置在 `env.json` 的 `motor_engine_union_env` 中。示例可参考 `examples/infer_engines/vllm/pd_hybrid/env.json`。

**配置示例**：

```json
"motor_engine_union_env": {
  "HCCL_BUFFSIZE": 200,
  "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
  "HCCL_OP_EXPANSION_MODE": "AIV",
  "OMP_PROC_BIND": "false",
  "OMP_NUM_THREADS": 100,
  "ASCEND_BUFFER_POOL": "0:0"
}
```

| 配置项 | 说明 |
|--------|------|
| motor_common_env | 所有组件共用环境变量，如 CANN 安装路径、日志根目录 |
| motor_engine_union_env | PD 混部 union 实例的 NPU、HCCL、OMP 等环境变量，可按机型与模型进行调优 |
