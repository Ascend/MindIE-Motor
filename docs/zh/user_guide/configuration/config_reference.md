# user_config.json配置文件全量参数说明

本文档详细说明user_config.json配置文件中Controller、Coordinator等组件的全量可配置项，其结构与“examples/features/config_sample.json"结构一一对应。
部署时，系统会将"user_config.json"中对应模块合并至组件运行时配置，遵循“代码默认值优先，用户配置覆盖”原则。此外，支持通过修改组件所监控的配置文件实现动态生效。配置文件位于“examples/infer_engines/”目录下（如“examples/infer_engines/vllm/user_config.json”），请根据实际使用的引擎类型和模型选择对应配置。

## motor_deploy_config

motor_deploy_config字段为部署与资源相关配置，由deploy.py读取并用于生成K8s资源、注入环境变量等。其配置样例如下所示：

```json
"motor_deploy_config": {
  "p_instances_num": 1,
  "d_instances_num": 1,
  "single_p_instance_pod_num": 1,
  "single_d_instance_pod_num": 1,
  "p_pod_npu_num": 16,
  "d_pod_npu_num": 16,
  "image_name": "",
  "job_id": "mindie-motor",
  "hardware_type": "800I_A3",
  "weight_mount_path": "/mnt/weight/",
  "tls_config": { ...
  }
}
```

**表1** motor_deploy_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------|
| p_instances_num | int | P实例个数，取值范围：[1,16] |
| d_instances_num | int | D实例个数，取值范围：[1,16] |
| single_p_instance_pod_num | int | 单个P实例对应的Pod数，取值范围：大于等于1 |
| single_d_instance_pod_num | int | 单个D实例对应的Pod数，取值范围：大于等于1 |
| p_pod_npu_num | int | 单个P实例Pod占用的NPU卡数，每个Pod最大为16卡 |
| d_pod_npu_num | int | 单个D实例Pod占用的NPU卡数，每个Pod最大为16卡 |
| image_name | string | 推理镜像名（需包含MindIE-PyMotor与vLLM等运行环境），与[PD分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md#setup-and-image-preparation)中准备/加载的镜像名保持一致 |
| job_id | string | 部署任务名，同时作为K8s命名空间使用，例如"mindie-motor" |
| hardware_type | string | 硬件类型：<ul><li>Atlas 800I A2 推理服务器：800I_A2</li><li>Atlas 800I A3 超节点服务器：800I_A3</li><li>Atlas 850 Server：850-Atlas-8p-8</li><li>Atlas 850 Server 超节点服务器：850-SuperPod-Atlas-8</li></ul>|
| weight_mount_path | string | 宿主机上模型权重挂载路径，容器内model_path需与此挂载路径一致，例如 `"/mnt/weight/"` |
| tls_config | object | 可选；TLS相关配置，包含mgmt_tls_config、infer_tls_config、etcd_tls_config、grpc_tls_config和observability_tls_config五类，结构见[PD分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md) |

---

## motor_controller_config

motor_controller_config字段配置样例如下所示：

```json
"motor_controller_config": {
  "logging_config": {
    "log_level": "INFO",
    "log_max_line_length": 8192,
    "log_format": "(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s",
    "log_date_format": "%m-%d %H:%M:%S",
    "host_log_dir": "/root/ascend/log",
    "log_rotation_size": 20,
    "log_rotation_count": 10,
    "log_compress": false,
    "log_compress_level": 6,
    "log_max_total_size": 200,
    "log_cleanup_interval": 1800,
    "third_party_log_levels": {
      "default": "WARNING"
    }
  },
  "api_config": {
    "controller_api_host": "127.0.0.1",
    "controller_api_dns": "mindie-motor-controller-service.mindie-motor.svc.cluster.local",
    "controller_api_port": 1026,
    "observability_api_port": 1027
  },
  "instance_config": {
    "instance_assemble_timeout": 600,
    "instance_assembler_check_interval": 1,
    "instance_assembler_cmd_send_interval": 1,
    "instance_manager_check_interval": 1,
    "instance_heartbeat_timeout": 10,
    "instance_expired_timeout": 1200,
    "send_cmd_retry_times": 3
  },
  "event_config": {
    "event_consumer_sleep_interval": 1.0,
    "coordinator_heartbeat_interval": 10.0
  },
  "fault_tolerance_config": {
    "enable_fault_tolerance": true,
    "strategy_center_check_interval": 1,
    "configmap_namespace": "kube-system",
    "configmap_prefix": "mindx-dl-deviceinfo-",
    "k8s_cert_path": "",
    "enable_scale_p2d": false,
    "enable_token_reinference": true,
    "scale_p2d_d_instance_reinit_wait_timeout": 60
  },
  "observability_config": {
    "observability_enable": false,
    "metrics_ttl": 5
  },
  "standby_config": {
    "enable_master_standby": false,
    "master_standby_check_interval": 5,
    "master_lock_ttl": 10,
    "master_lock_retry_interval": 5,
    "master_lock_max_failures": 3,
    "master_lock_key": "/controller/master_lock"
  },
  "etcd_config": {
    "etcd_host": "etcd.default.svc.cluster.local",
    "etcd_port": 2379,
    "etcd_timeout": 5,
    "etcd_lb_policy": "round_robin",
    "enable_etcd_persistence": false
  },
  "port_allocator_config": {
    "enable": true,
    "scan_range": 100,
    "probe_timeout_seconds": 0.5,
    "remote_check_timeout_seconds": 1.0,
    "bind_host": "0.0.0.0"
  },
  "precision_auto_recovery_enable": false
}
```

**表2** motor_controller_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| **logging_config字段** |-|-|
| log_level | string | 日志级别，默认值：INFO。<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| log_max_line_length | int | 单条日志最大长度，超过则截断。默认值：8192。 |
| log_format | string | 日志格式模板，支持Python logging 占位符。默认值："(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s"。 |
| log_date_format | string | 日志日期格式，默认值："%m-%d %H:%M:%S"。 |
|host_log_dir| string | 日志存储路径，默认值："/root/ascend/log"。|
| log_rotation_size | int | 日志转储文件大小，默认值：20。|
| log_rotation_count | int |日志转储文件个数，默认值：10。|
| log_compress |bool| 是否启动日志压缩，默认值：false。|
| log_compress_level |int|日志压缩层级，默认值：6。|
| log_max_total_size |int|日志文件总大小，单位：MB，默认值：200。|
| log_cleanup_interval |int|日志清理间隔，单位：秒，默认值：1800。|
| third_party_log_levels |string|第三方日志级别，默认值：WARNING。<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| **api_config字段** |-|-|
| controller_api_host | string | Controller API监听地址（IP 或主机名），默认值：127.0.0.1（或Env.pod_ip）。 |
| controller_api_dns |string|Controller API域名，默认值："mindie-motor-controller-service.mindie-motor.svc.cluster.local"。|
| controller_api_port | int | Controller API端口，默认值：1026。 |
| observability_api_port |int|Controller可观测性API端口，默认值：1027。|
| **instance_config字段** |-|-|
| instance_assemble_timeout | int | 等待实例就绪的最长等待时间，单位：秒，默认值：600。 |
| instance_assembler_check_internal | int | 轮询实例组装状态的间隔，单位：秒，默认值：1。 |
| instance_assembler_cmd_send_internal | int | 向实例下发组装命令的间隔，单位：秒，默认值：1。 |
| instance_manager_check_internal | int | 实例状态巡检间隔，单位：秒，默认值：1。 |
| instance_heartbeat_timeout | int | 超过该时长未收到实例心跳则判定异常，单位：秒，默认值：10。 |
| instance_expired_timeout | int | 实例空闲超过该时长则被清理，单位：秒，默认值：300。 |
| send_cmd_retry_times | int | 向实例下发命令失败时的重试次数，默认值：3。 |
| **event_config字段** |-|-|
| event_consumer_sleep_interval | float | 事件队列轮询间隔，即每次处理事件后的等待时间，单位：秒，默认值：1.0。 |
| coordinator_heartbeat_interval | float | Controller 与 Coordinator 间心跳上报间隔，单位：秒，默认值：10.0。 |
|<a id="fault_tolerance_config"></a>**fault_tolerance_config字段**|-|-|
| enable_fault_tolerance | bool | 是否启用故障自愈（高级 RAS），默认值：false。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul> |
| strategy_center_check_internal | int | 策略中心轮询间隔，单位：秒，默认值：1。 |
| configmap_namespace |string|configmap命名空间，默认值："kube-system"。|
| configmap_prefix |string|configmap前缀，默认值："mindx-dl-deviceinfo-"。|
| k8s_cert_path |string|安全证书路径，默认为空。|
| enable_scale_p2d | bool | 是否启用ScaleP2D弹性扩缩容，默认值：false。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul> |
| enable_token_reinference | bool | 是否启用Token Reinference 故障恢复，默认值：false。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul> |
| scale_p2d_d_instance_reinit_wait_timeout |int|ScaleP2D执行抢占前，等待D实例自恢复（重初始化）的最长时间，单位：秒，默认值：60。<br>等待期间若D实例恢复为initial/active，则不再执行ScaleP2D；超时后若D实例仍处于inactive等可抢占状态，则继续后续P实例选择流程。|
|**observability_config字段**|-|-|
| observability_enable |bool|是否启用可观测性，默认值：false。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul>|
| metrics_ttl |int|metrics查询间隔，单位：秒，默认值：5。|
| **standby_config字段**|-|-|
| enable_master_standby | bool | 是否开启 Controller 主备。可选：`true` / `false`。默认：`false` |
| master_standby_check_interval | int | 主备角色探测间隔（秒）。默认：`5` |
| master_lock_ttl | int | 主节点在 ETCD 上占锁的租约时长（秒）。默认：`10` |
| master_lock_retry_interval | int | 抢主时获取锁的重试间隔（秒）。默认：`5` |
| master_lock_max_failures | int | 连续抢主失败超过此次数则放弃并切换。默认：`3` |
| master_lock_key | string | 主节点在 ETCD 中的锁路径；运行时会自动加前缀 `/controller/`。默认：`/master_lock`（实际为 `/controller/master_lock`） |
| **etcd_config字段** |-|-|
| etcd_host | string | ETCD 服务地址（主机名或 IP）。默认：`etcd.default.svc.cluster.local` |
| etcd_port | int | ETCD 端口。默认：`2379` |
| etcd_timeout | int | ETCD 操作超时时间（秒）。默认：`5` |
|etcd_lb_policy|string|ETCD负载均衡策略，默认值：round_robin。|
| enable_etcd_persistence | bool | 是否启用 ETCD 持久化。可选：`true` / `false`。默认：`false` |
| **port_allocator_config字段** |-|-|
| enable |bool|是否使能端口自动分配，默认值：true。|
| scan_range |int|端口扫描范围，默认值：100.|
| probe_timeout_seconds |float|探测超时时间，默认值：0.5。|
| remote_check_timeout_seconds |float|远程检测超时时间，默认值：1.0。|
| bind_host |string|绑定主机地址，默认值：0.0.0.0。|

---

## motor_coordinator_config

motor_coordinator_config字段配置样例如下所示：

```json
"motor_coordinator_config": {
  "logging_config": {
    "log_level": "INFO",
    "log_max_line_length": 8192,
    "log_file": null,
    "log_format": "(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s",
    "log_date_format": "%m-%d %H:%M:%S",
    "host_log_dir": "/root/ascend/log",
    "log_rotation_size": 20,
    "log_rotation_count": 10,
    "log_compress": false,
    "log_compress_level": 6,
    "log_max_total_size": 200,
    "log_cleanup_interval": 1800,
    "third_party_log_levels": {
      "default": "WARNING"
    }
  },
  "prometheus_metrics_config": {
    "reuse_time": 3,
    "enable_kv_store_metrics": false,
    "kv_store_metrics_endpoint": ""
  },
  "exception_config": {
    "max_retry": 5,
    "reschedule_enabled": true,
    "transport_max_retry": null,
    "retry_delay": 0.2,
    "first_token_timeout": 600,
    "infer_timeout": 3600,
    "upstream_error_body_max_bytes": 65536
  },
  "scheduler_config": {
    "scheduler_type": "load_balance",
    "enable_pd_separation_fallback_to_hybrid": true,
    "endpoint_instance_score_weight": 0.05,
    "kv_affinity_mode": "unified",
    "kv_affinity_load_weight": 1.0,
    "kv_affinity_overlap_credit": 1.0,
    "kv_affinity_prefill_load_scale": 1.0,
    "kv_affinity_load_gate_topn": 0
  },
  "inference_workers_config": {
    "num_workers": 4
  },
  "timeout_config": {
    "request_timeout": 30,
    "connection_timeout": 10,
    "read_timeout": 15,
    "write_timeout": 15,
    "keep_alive_timeout": 60
  },
  "api_key_config": {
    "enable_api_key": false,
    "valid_keys": [],
    "header_name": "Authorization",
    "key_prefix": "Bearer ",
    "skip_paths": [
      "/",
      "/docs",
      "/favicon.ico",
      "/instances/refresh",
      "/liveness",
      "/metrics",
      "/openapi.json",
      "/readiness",
      "/redoc",
      "/startup"
    ],
    "encryption_algorithm": "PBKDF2_SHA256"
  },
  "rate_limit_config": {
    "enable_rate_limit": false,
    "provider": "simple",
    "max_requests": 1000,
    "window_size": 60,
    "scope": "global",
    "skip_paths": [
      "/docs",
      "/favicon.ico",
      "/liveness",
      "/metrics",
      "/openapi.json",
      "/readiness",
      "/redoc",
      "/startup"
    ],
    "error_message": "too many requests, please try again later",
    "error_status_code": 429,
    "olc_config_path": ""
  },
  "standby_config": {
    "enable_master_standby": false,
    "master_standby_check_interval": 5,
    "master_lock_ttl": 10,
    "master_lock_retry_interval": 5,
    "master_lock_max_failures": 3,
    "master_lock_key": "/coordinator/master_lock"
  },
  "etcd_config": {
    "etcd_host": "etcd.default.svc.cluster.local",
    "etcd_port": 2379,
    "etcd_timeout": 5,
    "etcd_lb_policy": "round_robin",
    "enable_etcd_persistence": false
  },
  "aigw_model": null,
  "api_config": {
    "coordinator_api_host": "127.0.0.1",
    "coordinator_api_dns": "mindie-motor-coordinator-service.mindie-motor.svc.cluster.local",
    "coordinator_api_infer_dns": "mindie-motor-coordinator-service.mindie-motor.svc.cluster.local",
    "coordinator_api_obs_dns": "mindie-motor-coordinator-service.mindie-motor.svc.cluster.local",
    "coordinator_api_infer_port": 1025,
    "coordinator_api_mgmt_port": 1026,
    "coordinator_obs_port": 1027
  },
  "tracer_config": {
    "endpoint": "",
    "root_sampling_rate": 1.0,
    "remote_parent_sampled": 1.0,
    "remote_parent_not_sampled": 1.0,
    "local_parent_sampled": 1.0,
    "local_parent_not_sampled": 1.0
  },
  "prefill_kv_event_config": {
    "conductor_service": "",
    "http_server_port": 13333,
    "block_size": 128,
    "endpoint": "",
    "replay_endpoint": "",
    "engine_type": "vLLM",
    "model_path": "",
    "re_register_interval_sec": 0
  },
  "token_sampling_config": {
    "interval_seconds": 30.0,
    "logprobs_count": 1,
    "precision_check_enabled": false,
    "precision_issue_threshold": 10,
    "probe_max_attempts": 3,
    "probe_timeout_seconds": 600.0
  },
  "port_allocator_config": {
    "enable": true,
    "scan_range": 100,
    "probe_timeout_seconds": 0.5,
    "remote_check_timeout_seconds": 1.0,
    "bind_host": "0.0.0.0"
  },
  "_errors": [],
  "worker_index": null
}
```

**表3** motor_coordinator_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| log_level | string | 日志级别。默认值：INFO<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul> |
| log_max_line_length | int | 单行日志最大长度，超过则截断。默认值：8192 |
| log_format | string | 日志格式模板，支持 Python logging 占位符。默认值："(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s" |
| log_date_format | string | 日志日期格式。默认值："%m-%d %H:%M:%S" |
|host_log_dir| string | 日志存储路径，默认值："/root/ascend/log"。|
| log_rotation_size | int | 日志转储文件大小，默认值：20。|
| log_rotation_count | int |日志转储文件个数，默认值：10。|
| log_compress |bool| 是否启动日志压缩，默认值：false。|
| log_compress_level |int|日志压缩层级，默认值：6。|
| log_max_total_size |int|日志文件总大小，单位：MB，默认值：200。|
| log_cleanup_interval |int|日志清理间隔，单位：秒，默认值：1800。|
| third_party_log_levels |string|第三方日志级别，默认值：WARNING。<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| **prometheus_metrics_config字段** |-|-|
| reuse_time | int | 后台采集周期，单位为秒，默认值：3。 |
| enable_kv_store_metrics |bool|是否拉取 KV 池化后端（MemCache / Mooncake）的 metrics。配置了 `kv_cache_store_config` 时自动启用，无需手动开启；该字段仅在需要显式覆盖自动行为时使用，默认值：false。|
| kv_store_metrics_endpoint |string|KV 池化 metrics 的 URL。配置了 `kv_cache_store_config` 时自动拼接（`http://{KVS_MASTER_SERVICE}:{KV_CACHE_STORE_PORT}/metrics`），无需手动配置；该字段仅在需要显式覆盖自动生成的 URL 时使用，默认值为空。|
| **exception_config字段** |-|-|
| max_retry | int | 请求失败后的最大重试次数。默认：`5` |
| reschedule_enabled | bool | 是否缓存流式响应 token ID，以便瞬时传输故障后重调度并续接请求。该配置不控制引擎侧 recompute，默认值：`true`。 <br>`recompute_enabled` 仅作为 `reschedule_enabled` 的旧配置兼容别名；`recompute_max_retry` 已移除并会被忽略。模型重计算由引擎侧负责。<br> 流式请求会在上游接受请求后再提交 HTTP 200；Unified PD 模式需等待 Prefill 和 Decode 两路均接受请求。提交前的引擎错误会保留原 HTTP 状态码、受限大小的响应体以及安全响应头；提交后 HTTP 状态码已不可修改，引擎 JSON 错误体会作为 SSE `data` 事件返回。|
| transport_max_retry | int/null | Coordinator 传输失败的最大尝试次数；`null` 时使用 `max_retry`。默认：`null` |
| retry_delay | float | 每次重试前的等待时间（秒）。默认：`0.2` |
| first_token_timeout | int | 等待首 token 返回的超时时间（秒）。默认：`600` |
| infer_timeout | int | 单次推理请求的总超时时间（秒）。默认：`3600` |
| upstream_error_body_max_bytes | int | 向客户端透传引擎 HTTP 错误体的最大字节数，避免返回超大错误响应。默认：`65536` |
| **scheduler_config字段** |-|-|
| scheduler_type | string | 调度类型，默认值：load_balance<ul><li>load_balance：负载均衡；</li><li>round_robin：轮询；</li><li>kv_cache_affinity：KV Cache 亲和调度。</li></ul> |
| enable_pd_separation_fallback_to_hybrid | bool | PD分离场景下，当D实例不可用或P/D实例不满足调度条件时，是否允许降级使用混部路由，默认值为 `true` |
| endpoint_instance_score_weight | float | endpoint 优先负载均衡时实例平均负载权重。默认：`0.05` |
| kv_affinity_mode | string | `scheduler_type=kv_cache_affinity` 时的子策略：`unified`（默认）或 `load_gated` |
| kv_affinity_load_weight | float | unified 模式下 endpoint 实时负载权重。默认：`1.0` |
| kv_affinity_overlap_credit | float | 缓存前缀对 prefill 成本的折扣系数。默认：`1.0` |
| kv_affinity_prefill_load_scale | float | unified 模式下（经亲和折扣后的）prefill 成本权重。默认：`1.0` |
| kv_affinity_load_gate_topn | int | load_gated 模式下先保留负载最低的 N 个 endpoint 再做亲和择优；`0` 时回退为 `2`。默认：`0` |
| **inference_workers_config字段** |-|-|
| num_workers | int | Coordinator中业务面worker个数，默认值：4。 |
| **timeout_config字段** |-|-|
| request_timeout | int | 单次 HTTP 请求超时时间（秒）。默认：`30` |
| connection_timeout | int | 建立连接的超时时间（秒）。默认：`10` |
| read_timeout | int | 读操作超时时间（秒）。默认：`15` |
| write_timeout | int | 写操作超时时间（秒）。默认：`15` |
| keep_alive_timeout | int | 连接保活时长，超时无活动则关闭（秒）。默认：`60` |
| **api_key_config字段** |-|-|
| enable_api_key | bool | 是否开启 API Key 鉴权。可选：`true` / `false`。默认：`false` |
| valid_keys | array | 合法的 API Key 字符串列表。默认：`[]` |
| header_name | string | 携带 API Key 的 HTTP 头名称。默认：`Authorization` |
| key_prefix | string | 头中 Key 的前缀，如`Bearer`。默认：`Bearer`|
| skip_paths | array | 不校验 API Key 的路径列表（如 `/metrics`、`/liveness`、`/docs` 等），可自定义 |
| encryption_algorithm | string | Key 校验使用的加密算法，如 `PBKDF2_SHA256`。默认：`PBKDF2_SHA256` |
| **rate_limit_config字段** |-|-|
| enable_rate_limit | bool | 是否开启请求限流。可选：`true` / `false`。默认：`false` |
| provider |string|限流提供者。simple使用内置令牌桶；OLC使用过载控制库（需额外安装及配置）。|
| max_requests | int | 限流时间窗口内允许的最大请求数。默认：`1000` |
| window_size | int | 限流统计的时间窗口长度（秒）。默认：`60` |
| scope | string | 限流生效范围，如 `global`（全局）。默认：`global` |
| skip_paths | array | 不参与限流统计的路径列表（如 `/liveness`、`/readiness`、`/metrics`），可自定义 |
| error_message | string | 触发限流时返回给客户端的提示文案。默认：`too many requests, please try again later` |
| error_status_code | int | 触发限流时返回的 HTTP 状态码，通常为 4xx（如 429）。默认：`429` |
| olc_config_path |string|OLC规则配置目录的绝对路径或相对于服务启动目录的相对路径。目录下需包含overload-config.properties和olc.json。|
| **standby_config字段** |-|-|
| enable_master_standby | bool | 是否开启 Coordinator 主备。可选：`true` / `false`。默认：`false` |
| master_standby_check_interval | int | 主备角色探测间隔（秒）。默认：`5` |
| master_lock_ttl | int | 主节点在 ETCD 上占锁的租约时长（秒）。默认：`10` |
| master_lock_retry_interval | int | 抢主时获取锁的重试间隔（秒）。默认：`5` |
| master_lock_max_failures | int | 连续抢主失败超过此次数则放弃并切换。默认：`3` |
| master_lock_key | string | 主节点在 ETCD 中的锁路径；运行时会自动加前缀 `/coordinator/`。默认：`/master_lock`（实际为 `/coordinator/master_lock`） |
| **etcd_config字段** |-|-|
| etcd_host | string |ETCD 服务地址（主机名或 IP）。默认：`etcd.default.svc.cluster.local` |
| etcd_port | int | ETCD 端口。默认：`2379` |
| etcd_timeout | int | ETCD 操作超时时间（秒）。默认：`5` |
|etcd_lb_policy|string|ETCD负载均衡策略，默认值：round_robin。
| enable_etcd_persistence | bool | 是否启用 ETCD 持久化。可选：`true` / `false`。默认：`false` |
| **aigw_model字段** |-|该参数是AIGW模型元数据的集中配置，用于/v1/models等接口返回的模型信息。在user_config.json中对应motor_coordinator_config下的aigw对象；未使用时为null，其内部可配置项如下所示。|
| id | string | 模型 ID，与 OpenAI 兼容接口中的模型名一致。若配置了 Prefill/Decode 的 model_name，部署时会自动填充为 Prefill 的 model_name |
| object | string | 对象类型，固定为 `model`。部署时未配置则自动填充 |
| owned_by | string | 模型归属标识，如 `motor`。部署时未配置则自动填充 |
| p_max_seqlen | int | Prefill 端最大序列长度（正整数）。未配置时从 Prefill 的 `engine_config.max_model_len` 自动填充 |
| d_max_seqlen | int | Decode 端最大序列长度（正整数）。未配置时从 Decode 的 `engine_config.max_model_len` 自动填充 |
| slo_ttft | int | 首 token 时延 SLO（毫秒），用于调度/监控。默认：`1000` |
| slo_tpot | int | 每 token 时延 SLO（毫秒），用于调度/监控。默认：`50` |
| **api_config字段** |-|-|
| coordinator_api_host | string | Coordinator API 监听地址（IP 或主机名），默认值：`127.0.0.1`（或 Env.pod_ip）。 |
| coordinator_api_dns | string | Coordinator管理面 API 域名，默认值：mindie-motor-coordinator-service.mindie-motor.svc.cluster.local。 |
| coordinator_api_infer_dns | string | Coordinator业务面 API 域名，默认值：mindie-motor-coordinator-service.mindie-motor.svc.cluster.local。 |
| coordinator_api_obs_dns | string | Coordinator可观测性 API 域名，默认值：mindie-motor-coordinator-service.mindie-motor.svc.cluster.local。 |
| coordinator_api_infer_port | int | 推理面端口。默认：`1025` |
| coordinator_api_mgmt_port | int | 管控面端口。默认：`1026` |
| coordinator_obs_port | int | Observability 端口，承载 `/metrics` 等可观测性接口。默认：`1027` |
| **tracer_config字段** |-|-|
| endpoint |string|链路追踪数据的上报地址或后端服务的接入点，默认值为空。|
| root_sampling_rate |float|根采样率，针对没有父Span（即请求的入口点，如HTTP请求的第一次进入）的追踪数据的采样概率。默认值为1.0，表示所有新的根请求都会被记录。如果设置为0.5，则只有一半的新请求会被记录，另一半则会被丢弃。|
| remote_parent_sampled |float|远程父采样率（当父Span被采样时），当前Span的父Span来自另一个服务（远程调用），且远程的父Span已经被采样时，当前Span的采样概率。默认值：1.0，表示当前调用100%被记录。|
| remote_parent_not_sampled |float|远程父采样率（当父Span未被采样时），当前Span的父Span来自另一个服务（远程调用），但远程的父Span没有被采样时，当前Span的采样概率。默认值：1.0，表示当前调用100%被记录。|
| local_parent_sampled |float|本地父采样率（当父Span被采样时），当前Span的父Span来自同一个服务实例内（本地调用），且父Span已经被采样时，当前Span的采样概率。默认值：1.0，表示当前调用100%被记录。|
| local_parent_not_sampled |float|本地父采样率（当父Span未被采样时），当前Span的父Span来自同一个服务实例内（本地调用），但父Span未被采样时，当前Span的采样概率。默认值：1.0，表示当前调用100%被记录。|
| **prefill_kv_event_config字段** |-|-|
| conductor_service |string|conductor服务IP或域名，默认为空。|
| http_server_port |int|KV Conductor的HTTP服务端口，默认值：13333，取值范围：[1024,65535]。|
| block_size |int|KV Cache块大小，默认值：128。|
| endpoint |string|P实例发布事件端点，默认为空，取值示例：tcp://*:\<port>。|
| replay_endpoint |string|事件回放端点，默认为空，取值示例：tcp://*:\<port>。|
| engine_type |string|引擎类型，默认值：vLLM。|
| model_path |string|模型权重路径，默认为空。|
|re_register_interval_sec|int|重注册时间间隔，默认值：0。|
| **token_sampling_config字段** |-|-|
| interval_seconds |float|每次采样的间隔时间，默认值：30.0。|
| logprobs_count |int|采样时需要带回多少log_prob，默认值：1。取值如下：<ul><li>1：只能检测重复。</li><li>3：可以检测重复和乱码。</li><li>5：可以检测重复、乱码和生僻字。</li></ul>|
| precision_check_enabled |bool|是否开启精度异常检测，默认值：false。|
| precision_issue_threshold |int|连续多少次异常会被判定为精度异常并触发上报，默认值：10。|
| probe_max_attempts |int|发现精度异常后会进行拔测得次数， 默认值：3。|
| probe_timeout_seconds |float|拔测一次的超时时间设置，默认值：600.0。|
| **port_allocator_config字段** |-|-|
| enable |bool|是否使能端口自动分配，默认值：true。|
| scan_range |int|端口扫描范围，默认值：100.|
| probe_timeout_seconds |float|探测超时时间，默认值：0.5。|
| remote_check_timeout_seconds |float|远程检测超时时间，默认值：1.0。|
| bind_host |string|绑定主机地址，默认值：0.0.0.0。|
| **request_limit字段** |-|config_sample.json中未包含此字段，但PD部署时常用，合并到运行时配置后生效。|
| single_node_max_requests | int | 单节点允许的最大并发请求数，由 user_config 配置 |
| max_requests | int | 集群全局最大并发请求数，由 user_config 配置 |

---

## motor_engine_union_config

motor_engine_union_config字段用于**PD混部场景**，配置同一类union Engine Server实例。其结构与motor_engine_prefill_config/motor_engine_decode_config类似，但不区分P/D两套引擎配置，也无需配置 kv_transfer_config的producer/consumer角色。其配置样例如下所示。

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
    "data_parallel_rpc_port": 9000,
    "enable_expert_parallel": false,
    "enforce-eager": true,
    "max_model_len": 2048,
    "kv_transfer_config": {
      "kv_connector": "MooncakeLayerwiseConnector",
      "kv_buffer_device": "npu",
      "kv_parallel_size": 1,
      "kv_port": "30001",
      "kv_connector_extra_config": {}
    }
  },
  "motor_nodemanger_config": {
    "api_config": {
      "pod_ip": "127.0.0.1",
      "node_manager_port": 1026
    },
    "endpoint_config": {
      "endpoint_num": 0,
      "base_port": 10000,
      "mgmt_ports": [],
      "service_ports": []
    },
    "basic_config": {...
    },
    "snapshot_config": {
      "enable_snapshot": false,
      "snapshot_metadata_path": ""
    },
    "logging_config": {
      "log_level": "INFO",
      "log_max_line_length": 8192,
      "log_format": "(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s",
      "log_date_format": "%m-%d %H:%M:%S",
      "host_log_dir": "/root/ascend/log",
      "log_rotation_size": 20,
      "log_rotation_count": 10,
      "log_compress": false,
      "log_compress_level": 6,
      "log_max_total_size": 200,
      "log_cleanup_interval": 1800,
      "log_collector_enabled": true,
      "third_party_log_levels": {
        "default": "WARNING"
      }
    },
    "single_container_config": {...
    },
    "fault_tolerance_config": {...
    },
    "port_allocator_config": {
      "enable": true,
      "scan_range": 100,
      "probe_timeout_seconds": 0.5,
      "remote_check_timeout_seconds": 1.0,
      "bind_host": "0.0.0.0"
    }
  }
}
```

**表4** <a id="motor_nodemanger_config"></a>motor_engine_union_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| engine_type | string | 引擎类型，如 `vllm` |
| **engine_config字段** | - |engine_config字段中的参数说明详情请参见[vLLM官网参数配置](https://docs.vllm.ai/en/latest/api/vllm/config)|
| **motor_nodemanger_config字段** |-|-|
| api_config.pod_ip |string | Pod IP（由环境或部署注入）。默认：`127.0.0.1`（或 Env.pod_ip） |
| api_config.node_manager_port |int | NodeManager 端口。默认：`1026` |
| endpoint_config.endpoint_num |int | 引擎端点数量，通常由 HCCL/并行配置推导。默认：`0` |
| endpoint_config.base_port |int | 端点端口起始号。默认：`10000` |
| endpoint_config.mgmt_ports |array | 各端点管控端口列表（整数数组）。默认：`[]` |
| endpoint_config.service_ports |array | 各端点推理服务端口列表（整数数组）。默认：`[]` |
| snapshot_config.enable_snapshot |bool|是否使能容器快照功能总开关，默认值：false。<br>开启后，用户可对实例容器制作快照镜像，并支持由快照恢复的实例向控制面注册。|
| snapshot_config.snapshot_metadata_path |string|容器快照元数据文件路径，包含容器快照制作与恢复过程中依赖的元数据，默认值为空。|
| logging_config.log_level | string | 日志级别，默认值：INFO<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| logging_config.log_max_line_length | int | 单条日志最大长度，超过则截断。默认值：8192 |
| logging_config.log_format | string | 日志格式模板，支持Python logging 占位符。默认值："(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s" |
| logging_config.log_date_format | string | 日志日期格式，默认值："%m-%d %H:%M:%S" |
| logging_config.host_log_dir | string | 日志存储路径，默认值："/root/ascend/log"。|
| logging_config.log_rotation_size | int | 日志转储文件大小，默认值：20。|
| logging_config.log_rotation_count | int |日志转储文件个数，默认值：10。|
| logging_config.log_compress |bool| 是否启动日志压缩，默认值：false。|
| logging_config.log_compress_level |int|日志压缩层级，默认值：6。|
| logging_config.log_max_total_size |int|日志文件总大小，单位：MB，默认值：200。|
| logging_config.log_cleanup_interval |int|日志清理间隔，单位：秒，默认值：1800。|
| logging_config.log_collector_enabled |bool|是否使能Collector日志，默认值：true。|
| logging_config.third_party_log_levels |string|第三方日志级别，默认值：WARNING。<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| port_allocator_config.enable |bool|是否使能端口自动分配，默认值：true。|
| port_allocator_config.scan_range |int|端口扫描范围，默认值：100.|
| port_allocator_config.probe_timeout_seconds |float|探测超时时间，默认值：0.5。|
| port_allocator_config.remote_check_timeout_seconds |float|远程检测超时时间，默认值：1.0。|
| port_allocator_config.bind_host |string|绑定主机地址，默认值：0.0.0.0。|

---

## motor_engine_prefill_config/motor_engine_decode_config

motor_engine_prefill_config和motor_engine_decode_config字段用于**PD分离部署场景**，这两个字段分别配置Prefill与Decode引擎。两者结构相同，均需指定engine_type与engine_config；可选配置dispatch_profile（PD协同语义）与health_check_config（虚推健康探测，见 [虚推健康探测](../features/sim_inference.md)）。配置示例如下所示。

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "gpu_memory_utilization": 0.9,
    "data_parallel_size": 1,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "data_parallel_rpc_port": 9000,
    "enable_expert_parallel": false,
    "enforce-eager": true,
    "max_model_len": 2048,
    "kv_transfer_config": {
      "kv_connector": "MooncakeLayerwiseConnector",
      "kv_buffer_device": "npu",
      "kv_role": "kv_producer",
      "kv_parallel_size": 1,
      "kv_port": "30001",
      "engine_id": "0",
      "kv_rank": 0,
      "kv_connector_extra_config": {}
    }
  },
  "motor_nodemanger_config": {
    "api_config": {
      "pod_ip": "127.0.0.1",
      "node_manager_port": 1026
    },
    "endpoint_config": {
      "endpoint_num": 0,
      "base_port": 10000,
      "mgmt_ports": [],
      "service_ports": []
    },
    "basic_config": {...
    },
    "snapshot_config": {
      "enable_snapshot": false,
      "snapshot_metadata_path": ""
    },
    "logging_config": {
      "log_level": "INFO",
      "log_max_line_length": 8192,
      "log_format": "(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s",
      "log_date_format": "%m-%d %H:%M:%S",
      "host_log_dir": "/root/ascend/log",
      "log_rotation_size": 20,
      "log_rotation_count": 10,
      "log_compress": false,
      "log_compress_level": 6,
      "log_max_total_size": 200,
      "log_cleanup_interval": 1800,
      "log_collector_enabled": true,
      "third_party_log_levels": {
        "default": "WARNING"
      }
    },
    "single_container_config": {...
    },
    "fault_tolerance_config": {...
    },
    "port_allocator_config": {
      "enable": true,
      "scan_range": 100,
      "probe_timeout_seconds": 0.5,
      "remote_check_timeout_seconds": 1.0,
      "bind_host": "0.0.0.0"
    }
  }
},
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "gpu_memory_utilization": 0.9,
    "data_parallel_size": 1,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "data_parallel_rpc_port": 9000,
    "enable_expert_parallel": false,
    "enforce-eager": true,
    "max_model_len": 2048,
    "kv_transfer_config": {
      "kv_connector": "MooncakeLayerwiseConnector",
      "kv_buffer_device": "npu",
      "kv_role": "kv_producer",
      "kv_parallel_size": 1,
      "kv_port": "30001",
      "engine_id": "0",
      "kv_rank": 0,
      "kv_connector_extra_config": {}
    }
  },
  "motor_nodemanger_config": {
    "api_config": {
      "pod_ip": "127.0.0.1",
      "node_manager_port": 1026
    },
    "endpoint_config": {
      "endpoint_num": 0,
      "base_port": 10000,
      "mgmt_ports": [],
      "service_ports": []
    },
    "basic_config": {...
    },
    "snapshot_config": {
      "enable_snapshot": false,
      "snapshot_metadata_path": ""
    },
    "logging_config": {
      "log_level": "INFO",
      "log_max_line_length": 8192,
      "log_format": "(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s",
      "log_date_format": "%m-%d %H:%M:%S",
      "host_log_dir": "/root/ascend/log",
      "log_rotation_size": 20,
      "log_rotation_count": 10,
      "log_compress": false,
      "log_compress_level": 6,
      "log_max_total_size": 200,
      "log_cleanup_interval": 1800,
      "log_collector_enabled": true,
      "third_party_log_levels": {
        "default": "WARNING"
      }
    },
    "single_container_config": {...
    },
    "fault_tolerance_config": {...
    },
    "port_allocator_config": {
      "enable": true,
      "scan_range": 100,
      "probe_timeout_seconds": 0.5,
      "remote_check_timeout_seconds": 1.0,
      "bind_host": "0.0.0.0"
    }
  }
}

```

**表5** motor_engine_prefill_config/motor_engine_decode_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| engine_type | string | 引擎类型，如 `vllm` |
| **engine_config字段** | - | engine_config字段中的参数说明详情请参见[vLLM官网参数配置](https://docs.vllm.ai/en/latest/api/vllm/config) |
| **motor_nodemanger_config字段** |-|-|
| api_config.pod_ip |string | Pod IP（由环境或部署注入）。默认：`127.0.0.1`（或 Env.pod_ip） |
| api_config.node_manager_port |int | NodeManager 端口。默认：`1026` |
| endpoint_config.endpoint_num |int | 引擎端点数量，通常由 HCCL/并行配置推导。默认：`0` |
| endpoint_config.base_port |int | 端点端口起始号。默认：`10000` |
| endpoint_config.mgmt_ports |array | 各端点管控端口列表（整数数组）。默认：`[]` |
| endpoint_config.service_ports |array | 各端点推理服务端口列表（整数数组）。默认：`[]` |
| snapshot_config.enable_snapshot |bool|是否使能容器快照功能总开关，默认值：false。<br>开启后，用户可对实例容器制作快照镜像，并支持由快照恢复的实例向控制面注册。|
| snapshot_config.snapshot_metadata_path |string|容器快照元数据文件路径，包含容器快照制作与恢复过程中依赖的元数据，默认值为空。|
| logging_config.log_level | string | 日志级别，默认值：INFO<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| logging_config.log_max_line_length | int | 单条日志最大长度，超过则截断。默认值：8192 |
| logging_config.log_format | string | 日志格式模板，支持Python logging 占位符。默认值："(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s" |
| logging_config.log_date_format | string | 日志日期格式，默认值："%m-%d %H:%M:%S" |
| logging_config.host_log_dir | string | 日志存储路径，默认值："/root/ascend/log"。|
| logging_config.log_rotation_size | int | 日志转储文件大小，默认值：20。|
| logging_config.log_rotation_count | int |日志转储文件个数，默认值：10。|
| logging_config.log_compress |bool| 是否启动日志压缩，默认值：false。|
| logging_config.log_compress_level |int|日志压缩层级，默认值：6。|
| logging_config.log_max_total_size |int|日志文件总大小，单位：MB，默认值：200。|
| logging_config.log_cleanup_interval |int|日志清理间隔，单位：秒，默认值：1800。|
| logging_config.log_collector_enabled |bool|是否使能Collector日志，默认值：true。|
| logging_config.third_party_log_levels |string|第三方日志级别，默认值：WARNING。<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| port_allocator_config.enable |bool|是否使能端口自动分配，默认值：true。|
| port_allocator_config.scan_range |int|端口扫描范围，默认值：100.|
| port_allocator_config.probe_timeout_seconds |float|探测超时时间，默认值：0.5。|
| port_allocator_config.remote_check_timeout_seconds |float|远程检测超时时间，默认值：1.0。|
| port_allocator_config.bind_host |string|绑定主机地址，默认值：0.0.0.0。|

PD模式下P与D**各自独立配置**"health_check_config"；未配置时使用代码默认值。引擎"engine_config"字段说明请参见[PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md#生成配置文件)。

### dispatch_profile

当engine_config.kv_transfer_config.kv_connector不在内置识别白名单内时，可在motor_engine_prefill_config/motor_engine_decode_config**顶层**显式声明 P/D 协同语义。NodeManager根据此推导并向Coordinator上报dispatch_capabilities。

**表6** dispatch_profile参数说明

| 配置项 | 类型 | 说明 |
|--------|------|--------|
| dispatch_profile | string | P/D 协同语义。默认值：未配时由 kv_connector白名单推断。<br>可选值：<ul><li>handoff：Prefill完成后交给Decode，推导出的capability为prefill_handoff_decode。</li><li>trigger：P/D并发，引擎同步KV，推导出的capability为concurrent_engine_sync。</li></ul>Prefill与Decode**两端须配置相同取值**。 |

vLLM内置识别的kv_connector白名单见[PD 分离特性说明](../../design/pd_disaggregation.md#vllm-connector-识别白名单)。白名单内connector无需手动配置dispatch_profile。

>[!NOTE]说明
>
>- dispatch_profile写在motor_engine_*_config顶层，不是在engine_config字段内部。
>- 不支持用户直接填写dispatch_capabilities，配置后会被NodeManager丢弃。
>- 取值须与connector实际协同语义一致；P/D不一致或无共同capability时，Coordinator路由返回503。

**配置示例**（自定义connector）：

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    ...
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_producer",
      ...
    }
  }
},
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    ...
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_consumer",
      ...
    }
  }
}
```

### health_check_config

可选虚推（虚拟推理）健康探测配置，位于 `motor_engine_prefill_config` / `motor_engine_decode_config` 子块，默认关闭。机制说明见 [虚推健康探测](../features/sim_inference.md)；PD 部署配置示例见 [PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md#virtual-inference-health-check)。

**表7** health_check_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|--------|
| enable_virtual_inference | bool | 虚推总开关，默认值：false。<br>取值为 `true` 时，在推理面 `/health` 正常后启动周期性虚推。**仅支持 vLLM**；SGLang 引擎配置为 `true` 时运行时会自动关闭 |
| npu_usage_threshold | int | AI Cube 利用率阈值（%），默认值：3。<br>虚推仅在 `0 < npu_usage_threshold <= 100` 时启动；低于该阈值且虚推失败时累计失败次数 |
| max_failure_count | int | 连续虚推失败次数上限（在累计条件满足后），默认值：6。<br>达到后Engine Server `/status` 返回abnormal。 |
| health_collector_timeout | int | 推理面 `GET /health` 探测超时（秒），默认值：5。 |
| health_collector_timeout_retry_attempts | int | 推理面 `GET /health` 超时重试次数（含首次），默认值：3。<br>仅在探测超时时重试；连接失败、HTTP 错误等其它异常不重试。 |

---

## 其他参数说明

### motor_engine_union_env字段

PD混部场景下，union Engine Server 的环境变量配置在 `env.json` 的 `motor_engine_union_env` 中。示例可参考 `examples/infer_engines/vllm/pd_hybrid/env.json`。

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

**表8** motor_engine_union_env字段参数说明

| 配置项 | 说明 |
|--------|------|
| motor_common_env | 所有组件共用环境变量，如CANN安装路径、日志根目录。 |
| motor_engine_union_env | PD混部union实例的NPU、HCCL、OMP等环境变量，可按机型与模型进行调优。 |

### prefill_kv_event_config 自动推导

该字段加载 `user_config.json` 时由 Coordinator 合并，一般无需手动添加。
Coordinator 会根据实例角色自动识别 P/D 分离或 union 混部拓扑，并根据引擎 Connector 推导、由 NodeManager 内部上报的 `dispatch_capabilities` 选择并发或 handoff 行为。该字段不支持用户显式配置；自定义 Connector 可在 `motor_engine_prefill_config` / `motor_engine_decode_config` 顶层使用 `dispatch_profile` 声明语义，详情请参见[dispatch_profile](#dispatch_profile)。
Connector 识别白名单、`MultiConnector` 取 `connectors[0]` 的规则，以及未识别连接器导致路由 503（fail-closed）的处理，详情请参见[PD 分离特性说明](../../design/pd_disaggregation.md#vllm-connector-识别白名单)与[PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md)。

**表9** prefill_kv_event_config说明

| 来源 | 说明 |
|------|------|
| PD 分离 | 从 `motor_engine_prefill_config.engine_config.kv-events-config` 推导 |
| PD 混部 | 从 `motor_engine_union_config.engine_config.kv-events-config` 推导 |
| kv_conductor_config | `http_server_port` 写入 `prefill_kv_event_config.http_server_port`；未配置时默认 `13333` |
