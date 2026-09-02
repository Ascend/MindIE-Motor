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
| image_name | string | 推理镜像名（需包含MindIE Motor与vLLM等运行环境） |
| job_id | string | 部署任务名，同时作为K8s命名空间使用，例如"mindie-motor" |
| hardware_type | string | 硬件类型：<ul><li>Atlas 800I A2 推理服务器：800I_A2</li><li>Atlas 800I A3 超节点服务器：800I_A3</li><li>Atlas 850 Server：850-Atlas-8p-8</li><li>Atlas 850 Server 超节点服务器：850-SuperPod-Atlas-8</li></ul>|
| weight_mount_path | string | 宿主机上模型权重挂载路径，容器内 `model` 路径需与此挂载路径一致，例如 `"/mnt/weight/"`。使用标准 deployer 时，该路径会同时挂载到 P/D（或 Union）引擎和 Coordinator；开启 `context_budget_mode: "on"` 后，Coordinator 也必须能够读取 `engine_config.model` 中的 tokenizer 文件。该字段使用 `hostPath`，因此 Coordinator 可能调度到的节点均需存在该路径，或通过 `coordinator_node_selector` 限制其调度范围。 |
| tls_config | object | 可选；TLS相关配置，包含mgmt_tls_config、infer_tls_config、etcd_tls_config、grpc_tls_config和observability_tls_config五类 |

---

## <a id="additional-labels-annotations"></a>组件级Kubernetes Labels和Annotations

以下组件配置支持在顶层通过 `additional_labels` 和 `additional_annotations` 为生成的Kubernetes资源添加自定义元数据：

| 组件配置 | 生效组件 |
|----------|----------|
| motor_controller_config | Controller |
| motor_coordinator_config | Coordinator |
| motor_engine_prefill_config | Prefill Engine |
| motor_engine_decode_config | Decode Engine |
| motor_engine_encode_config | Encode Engine |
| motor_engine_union_config | Union Engine |
| kv_cache_store_config | KV Cache Store |
| kv_conductor_config | KV Conductor |
| mf_store_config | MF Store |

配置示例如下所示：

```json
"motor_controller_config": {
  "additional_annotations": {
    "motor-component": "controller"
  },
  "additional_labels": {
    "motor-component": "controller"
  }
}
```

| 配置项 | 类型 | 说明 |
|--------|------|------|
| additional_annotations | object | 可选；需要添加到组件工作负载及其Pod模板 `metadata.annotations` 的键值对，默认不添加。自定义键与模板中已有键同名时，以此处配置的值为准。键和值需符合Kubernetes Annotation规范。 |
| additional_labels | object | 可选；需要添加到组件工作负载及其Pod模板 `metadata.labels` 的键值对，默认不添加。除部署器保留标签外，自定义键与模板中已有键同名时，以此处配置的值为准。键和值需符合Kubernetes Label规范。 |

上述字段仅修改工作负载及其Pod模板的元数据，不修改对应Service等其他资源。`single_container` 部署模式下，各组件共享同一个工作负载，自定义元数据通过 `motor_coordinator_config` 配置。

>[!NOTE]说明
>
>`app` 是部署器保留标签，不能通过 `additional_labels` 配置，部署主流程会在生成资源前拒绝此类配置。请勿覆盖其他由部署器生成的组件标识、工作负载选择器等系统Labels；本功能不会同步修改 `spec.selector`，覆盖此类Labels可能导致工作负载无法创建或无法匹配Pod。

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
  "precision_auto_recovery_enable": false,
  "additional_annotations": {
    "motor-component": "controller"
  },
  "additional_labels": {
    "motor-component": "controller"
  }
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
| host_log_dir | string | 日志存储路径，默认值："/root/ascend/log"。|
| log_rotation_size | int | 日志转储文件大小，默认值：20。|
| log_rotation_count | int | 日志转储文件个数，默认值：10。|
| log_compress |bool| 是否启动日志压缩，默认值：false。|
| log_compress_level |int| 日志压缩层级，默认值：6。|
| log_max_total_size |int| 日志文件总大小，单位：MB，默认值：200。|
| log_cleanup_interval |int| 日志清理间隔，单位：秒，默认值：1800。|
| third_party_log_levels | string | 第三方日志级别，默认值：WARNING。<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul>|
| **api_config字段** |-|-|
| controller_api_host | string | Controller API监听地址（IP 或主机名），默认值：127.0.0.1（或Env.pod_ip）。 |
| controller_api_dns | string | Controller API域名，默认值："mindie-motor-controller-service.mindie-motor.svc.cluster.local"。|
| controller_api_port | int | Controller API端口，默认值：1026。 |
| observability_api_port | int | Controller可观测性API端口，默认值：1027。|
| **instance_config字段** |-|-|
| instance_assemble_timeout | int | 等待实例就绪的最长等待时间，单位：秒，默认值：600。 |
| instance_assembler_check_interval | int | 轮询实例组装状态的间隔，单位：秒，默认值：1。 |
| instance_assembler_cmd_send_interval | int | 向实例下发组装命令的间隔，单位：秒，默认值：1。 |
| instance_manager_check_interval | int | 实例状态巡检间隔，单位：秒，默认值：1。 |
| instance_heartbeat_timeout | int | 超过该时长未收到实例心跳则判定异常，单位：秒，默认值：10。 |
| instance_expired_timeout | int | 实例空闲超过该时长则被清理，单位：秒，默认值：1200。 |
| send_cmd_retry_times | int | 向实例下发命令失败时的重试次数，默认值：3。 |
| **event_config字段** |-|-|
| event_consumer_sleep_interval | float | 事件队列轮询间隔，即每次处理事件后的等待时间，单位：秒，默认值：1.0。 |
| coordinator_heartbeat_interval | float | Controller 与 Coordinator 间心跳上报间隔，单位：秒，默认值：10.0。 |
|<a id="fault_tolerance_config"></a>**fault_tolerance_config字段**|-|-|
| enable_fault_tolerance | bool | 是否启用 Motor Controller 故障自愈（高级 RAS），默认值：true。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul>该字段不等价于、也不会自动透传为 vLLM 开发分支的 `--enable-fault-tolerance`。 |
| strategy_center_check_interval | int | 策略中心轮询间隔，单位：秒，默认值：1。 |
| configmap_namespace |string|configmap命名空间，默认值："kube-system"。|
| configmap_prefix |string|configmap前缀，默认值："mindx-dl-deviceinfo-"。|
| k8s_cert_path |string|安全证书路径，默认为空。|
| enable_scale_p2d | bool | 是否启用ScaleP2D弹性扩缩容，默认值：false。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul> |
| enable_token_reinference | bool | 是否启用Token Reinference 故障恢复，默认值：true。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul> |
| scale_p2d_d_instance_reinit_wait_timeout |int|ScaleP2D执行抢占前，等待D实例自恢复（重初始化）的最长时间，单位：秒，默认值：60。<br>等待期间若D实例恢复为initial/active，则不再执行ScaleP2D；超时后若D实例仍处于inactive等可抢占状态，则继续后续P实例选择流程。|
| **observability_config字段**|-|-|
| observability_enable |bool|是否启用可观测性，默认值：false。取值如下：<ul><li>true：启用</li><li>false：不启用</li></ul>|
| metrics_ttl |int|metrics查询间隔，单位：秒，默认值：5。|
| **standby_config字段**|-|-|
| enable_master_standby | bool | 是否开启 Controller 主备。可选：`true` / `false`。默认值：`false` |
| master_standby_check_interval | int | 主备角色探测间隔（秒）。默认值：`5` |
| master_lock_ttl | int | 主节点在 ETCD 上占锁的租约时长（秒）。默认值：`10` |
| master_lock_retry_interval | int | 抢主时获取锁的重试间隔（秒）。默认值：`5` |
| master_lock_max_failures | int | 连续抢主失败超过此次数则放弃并切换。默认值：`3` |
| master_lock_key | string | 主节点在 ETCD 中的锁路径；运行时会自动加前缀 `/controller/`。默认值：`/master_lock`（实际为 `/controller/master_lock`） |
| **etcd_config字段** |-|-|
| etcd_host | string | ETCD 服务地址（主机名或 IP）。默认值：`etcd.default.svc.cluster.local` |
| etcd_port | int | ETCD 端口。默认值：`2379` |
| etcd_timeout | int | ETCD 操作超时时间（秒）。默认值：`5` |
| etcd_lb_policy | string | ETCD负载均衡策略，默认值：round_robin。|
| enable_etcd_persistence | bool | 是否启用 ETCD 持久化。可选：`true` / `false`。默认值：`false` |
| **port_allocator_config字段** |-|-|
| enable |bool|是否使能端口自动分配，默认值：true。|
| scan_range |int|端口扫描范围，默认值：100.|
| probe_timeout_seconds |float|探测超时时间，默认值：0.5。|
| remote_check_timeout_seconds |float|远程检测超时时间，默认值：1.0。|
| bind_host |string|绑定主机地址，默认值：0.0.0.0。|
| additional_annotations | object | 可选；Controller工作负载及其Pod模板的自定义Annotations |
| additional_labels | object | 可选；Controller工作负载及其Pod模板的自定义Labels。 |

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
    "reschedule_config": {
      "enable": false
    },
    "max_retry": 5,
    "transport_max_retry": null,
    "retry_delay": 0.2,
    "first_token_timeout": 600,
    "infer_timeout": 3600,
    "upstream_error_body_max_bytes": 65536
  },
  "context_budget_mode": "off",
  "render_config": {
    "enabled": false,
    "endpoint": {
      "host": "127.0.0.1",
      "port": 8100
    },
    "timeout_ms": 5000,
    "image_name": ""
  },
  "scheduler_config": {
    "scheduler_type": "load_balance",
    "enable_pd_separation_fallback_to_hybrid": true,
    "endpoint_instance_score_weight": 0.05,
    "kv_affinity": {
      "mode": "unified",
      "load_weight": 1.0,
      "overlap_credit": 1.0,
      "prefill_load_scale": 1.0,
      "load_gate_topn": 0,
      "w_npu": 1.0,
      "w_cpu": 1.0,
      "w_disk": 0.0
    }
  },
  "inference_workers_config": {
    "num_workers": 4,
    "worker_metaserver_base_port": 12000
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
      "/startup",
      "/v1/metaserver"
    ],
    "encryption_algorithm": "PBKDF2_SHA256"
  },
  "mgmt_api_key_config": {
    "enable_api_key": false,
    "api_key_file": ""
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
      "/startup",
      "/v1/metaserver"
    ],
    "error_message": "too many requests, please try again later",
    "error_status_code": 429,
    "max_request_body_size": 0,
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
  "worker_index": null,
  "additional_annotations": {
    "motor-component": "coordinator"
  },
  "additional_labels": {
    "motor-component": "coordinator"
  }
}
```

**表3** motor_coordinator_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| context_budget_mode | string | 请求路由前使用模型 tokenizer 计算 prompt token 数，并将实际生效的 `max_tokens` / `max_completion_tokens` 裁剪到模型剩余上下文。适用于 `load_balance`、`round_robin`、`kv_cache_affinity`。可选值：`off`（默认）或 `on`；开启 `on` 时，P/D 引擎配置必须提供 `model` 与 `max_model_len`。 |
| **render_config字段** |-|-|
| enabled | bool | 是否启用 vLLM Render Sidecar。默认值：`false`。启用后，Coordinator 优先调用 Render；不支持的请求或调用失败会自动回退本地 tokenizer。 |
| endpoint | object | Render Sidecar 地址。`host` 默认值为 `127.0.0.1`，Sidecar 部署仅支持本机地址；`port` 默认值为 `8100`。 |
| timeout_ms | int | Render、Derender 与健康检查的 HTTP 超时时间，单位：毫秒。默认值：`5000`。 |
| image_name | string | Render Sidecar 镜像。配置后使用该独立镜像；为空时复用 `motor_deploy_config.image_name`。默认值为空。 |
| log_level | string | 日志级别。默认值：INFO<ul><li>DEBUG</li><li>INFO</li><li>WARNING</li><li>ERROR</li></ul> |
| log_max_line_length | int | 单行日志最大长度，超过则截断。默认值：8192 |
| log_format | string | 日志格式模板，支持 Python logging 占位符。默认值："(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s" |
| log_date_format | string | 日志日期格式。默认值："%m-%d %H:%M:%S" |
| host_log_dir| string | 日志存储路径，默认值："/root/ascend/log"。|
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
| max_retry | int | 请求失败后的最大重试次数。默认值：`5` |
| transport_max_retry | int/null | Coordinator 传输失败的最大尝试次数；`null` 时使用 `max_retry`。默认值：`null` |
| retry_delay | float | 每次重试前的等待时间（秒）。默认值：`0.2` |
| first_token_timeout | int | 等待首 token 返回的超时时间（秒）。默认值：`600` |
| infer_timeout | int | 单次推理请求的总超时时间（秒）。非流式场景作用于单次转发；流式场景作为整个流式请求的整体墙钟超时（从请求到达算起，超时后中断并返回 504）。默认：`3600` |
| upstream_error_body_max_bytes | int | 向客户端透传引擎 HTTP 错误体的最大字节数，避免返回超大错误响应。默认值：`65536` |
| **reschedule_config字段** |-|-|
| enable | bool | 故障场景重调度功能开关。默认值：`false`。<br>模型重计算由引擎侧负责，该配置不控制引擎侧重计算；`recompute_enabled`仅作为`reschedule_enabled`的旧配置兼容别名；`recompute_max_retry`已移除并会被忽略。 |
| **scheduler_config字段** |-|-|
| scheduler_type | string | 调度类型，默认值：load_balance<ul><li>load_balance：负载均衡；</li><li>round_robin：轮询；</li><li>kv_cache_affinity：KV Cache 亲和调度。</li></ul> |
| enable_pd_separation_fallback_to_hybrid | bool | PD分离场景下，当D实例不可用或P/D实例不满足调度条件时，是否允许降级使用混部路由，默认值为 `true` |
| endpoint_instance_score_weight | float | endpoint 优先负载均衡时实例平均负载权重。默认值：`0.05` |
| kv_affinity | object | KV Cache 亲和性调度参数（见下表） |
| **kv_affinity 字段** |-|-|
| mode | string | `scheduler_type=kv_cache_affinity` 时的子策略：`unified`（默认）或 `load_gated` |
| load_weight | float | unified 模式下 endpoint 实时负载权重。默认值：`1.0` |
| overlap_credit | float | 缓存前缀对 prefill 成本的折扣系数。默认值：`1.0` |
| prefill_load_scale | float | unified 模式下（经亲和折扣后的）prefill 成本权重。默认值：`1.0` |
| load_gate_topn | int | load_gated 模式下先保留负载最低的 N 个 endpoint 再做亲和择优；`0` 时回退为 `2`。默认值：`0` |
| w_npu | float | 互斥 NPU 命中块权重。默认值：`1.0` |
| w_cpu | float | 互斥 CPU 命中块权重。默认值：`1.0` |
| w_disk | float | 互斥 Disk 命中块权重。默认值：`0.0` |
| **inference_workers_config字段** |-|-|
| num_workers | int | Coordinator中业务面worker个数，默认值：4。 |
| worker_metaserver_base_port | int | vLLM layerwise/trigger PD 时每个 Inference Worker 的 metaserver 起始端口。默认值：`12000`。Worker `i` 监听 `base+i`，仅暴露 `POST /v1/metaserver`。设为 `0` 关闭。须保证 `base+num_workers-1 <= 65535`。同一集群不可混部 handoff 与 trigger。监听地址优先 `POD_IP`，否则用 `coordinator_api_host`（不绑 loopback）。`coordinator_api_host=0.0.0.0`/`::` 仍可启动；走 Trigger 时须有 `POD_IP` 或可达的 `coordinator_api_host`，否则该请求返回 503。端口占用或 metaserver 启动失败时推理口继续服务，该 Worker 的 Trigger 请求返回 503。 |
| **timeout_config字段** |-|-|
| request_timeout | int | 单次 HTTP 请求超时时间（秒）。默认值：`30` |
| connection_timeout | int | 建立连接的超时时间（秒）。默认值：`10` |
| read_timeout | int | 读操作超时时间（秒）。默认值：`15` |
| write_timeout | int | 写操作超时时间（秒）。默认值：`15` |
| keep_alive_timeout | int | 连接保活时长，超时无活动则关闭（秒）。默认值：`60` |
| **api_key_config字段** |-|-|
| enable_api_key | bool | 是否开启 API Key 鉴权。可选：`true` / `false`。默认值：`false` |
| valid_keys | array | 合法的 API Key 字符串列表。默认值：`[]` |
| header_name | string | 携带 API Key 的 HTTP 头名称。默认值：`Authorization` |
| key_prefix | string | 头中 Key 的前缀，如`Bearer`。默认值：`Bearer`|
| skip_paths | array | 不校验 API Key 的路径列表（如 `/metrics`、`/liveness`、`/docs`、`/v1/metaserver` 等），可自定义。代码默认包含 `/v1/metaserver`（Decode layerwise 回调不带 Key）。 |
| encryption_algorithm | string | Key 校验使用的加密算法，如 `PBKDF2_SHA256`。默认值：`PBKDF2_SHA256` |
| **mgmt_api_key_config字段** |-|管理面独立 API Key 配置，仅保护实例查询、实例刷新和精度告警状态清理接口。启动、存活和就绪探针不鉴权。请求头固定为 `X-Motor-Management-Key`。|
| enable_api_key | bool | 是否开启管理面 API Key 鉴权。可选：`true` / `false`。默认值：`false`。|
| api_key_file | string | API Key 文件路径。开启鉴权时必填；文件必须仅含一行非空密钥。Controller 与 Coordinator 分开部署时需挂载内容相同的密钥文件。|
| **rate_limit_config字段** |-|-|
| enable_rate_limit | bool | 是否开启请求限流。可选：`true` / `false`。默认值：`false` |
| provider |string|限流提供者。simple使用内置令牌桶；OLC使用过载控制库（需额外安装及配置）。|
| max_requests | int | 限流时间窗口内允许的最大请求数。默认值：`1000` |
| window_size | int | 限流统计的时间窗口长度（秒）。默认值：`60` |
| scope | string | 限流生效范围，如 `global`（全局）。默认值：`global` |
| skip_paths | array | 不参与限流统计的路径列表（如 `/liveness`、`/readiness`、`/metrics`），可自定义 |
| error_message | string | 触发限流时返回给客户端的提示文案。默认值：`too many requests, please try again later` |
| error_status_code | int | 触发限流时返回的 HTTP 状态码，通常为 4xx（如 429）。默认值：`429` |
| max_request_body_size | float | 请求体最大大小（MB），超过则直接拒绝并返回 413，不消耗限流令牌。`= 0` 表示不限制。支持小数（如 `0.5` 表示 0.5MB，1MB = 1024\*1024 字节）。默认值：`0`（不限制） |
| olc_config_path |string|OLC规则配置目录的绝对路径或相对于服务启动目录的相对路径。目录下需包含overload-config.properties和olc.json。|
| **standby_config字段** |-|-|
| enable_master_standby | bool | 是否开启 Coordinator 主备。可选：`true` / `false`。默认值：`false` |
| master_standby_check_interval | int | 主备角色探测间隔（秒）。默认值：`5` |
| master_lock_ttl | int | 主节点在 ETCD 上占锁的租约时长（秒）。默认值：`10` |
| master_lock_retry_interval | int | 抢主时获取锁的重试间隔（秒）。默认值：`5` |
| master_lock_max_failures | int | 连续抢主失败超过此次数则放弃并切换。默认值：`3` |
| master_lock_key | string | 主节点在 ETCD 中的锁路径；运行时会自动加前缀 `/coordinator/`。默认值：`/master_lock`（实际为 `/coordinator/master_lock`） |
| **etcd_config字段** |-|-|
| etcd_host | string |ETCD 服务地址（主机名或 IP）。默认值：`etcd.default.svc.cluster.local` |
| etcd_port | int | ETCD 端口。默认值：2379。 |
| etcd_timeout | int | ETCD 操作超时时间（秒）。默认值：5`。 |
| etcd_lb_policy | string| ETCD负载均衡策略，默认值：round_robin。|
| enable_etcd_persistence | bool | 是否启用 ETCD 持久化。可选：`true` / `false`。默认值：false。 |
| **aigw_model字段** |-|该参数是AIGW模型元数据的集中配置，用于/v1/models等接口返回的模型信息。在user_config.json中对应motor_coordinator_config下的aigw对象；未使用时为null，其内部可配置项如下所示。启动时若存在完整 Prefill/Decode 配置，或 PD 混部的 `motor_engine_union_config`，会**仅填充未显式配置的字段**（P+D 优先于 union）；混部场景下缺失的 `p_max_seqlen` 与 `d_max_seqlen` 均取自 union 的 `max_model_len`。|
| id | string | 模型 ID，与 OpenAI 兼容接口中的模型名一致。未配置时从 Prefill（或混部 union）的 model_name 自动填充；已显式配置则保留 |
| object | string | 对象类型，固定为 `model`。未配置则自动填充 |
| owned_by | string | 模型归属标识，如 `motor`。未配置则自动填充 |
| p_max_seqlen | int | Prefill 端最大序列长度（正整数）。未配置时从 Prefill（或混部 union）的 `engine_config.max_model_len` 自动填充 |
| d_max_seqlen | int | Decode 端最大序列长度（正整数）。未配置时从 Decode（或混部 union）的 `engine_config.max_model_len` 自动填充 |
| slo_ttft | int | 首 token 时延 SLO（毫秒），用于调度/监控。默认值：`1000` |
| slo_tpot | int | 每 token 时延 SLO（毫秒），用于调度/监控。默认值：`50` |
| **api_config字段** |-|-|
| coordinator_api_host | string | Coordinator API 监听地址（IP 或主机名），默认值：`127.0.0.1`（或 Env.pod_ip）。 |
| coordinator_api_dns | string | Coordinator管理面 API 域名，默认值：mindie-motor-coordinator-service.mindie-motor.svc.cluster.local。 |
| coordinator_api_infer_dns | string | Coordinator业务面 API 域名，默认值：mindie-motor-coordinator-service.mindie-motor.svc.cluster.local。 |
| coordinator_api_obs_dns | string | Coordinator可观测性 API 域名，默认值：mindie-motor-coordinator-service.mindie-motor.svc.cluster.local。 |
| coordinator_api_infer_port | int | 推理面端口。默认值：`1025` |
| coordinator_api_mgmt_port | int | 管控面端口。默认值：`1026` |
| coordinator_obs_port | int | Observability 端口，承载 `/metrics` 等可观测性接口。默认值：`1027` |
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
| query_encoding |string|Conductor `/query` 请求的传输编码，默认值：`msgpack`，取值：`msgpack` / `json`（启动时校验，非法值直接报错）。<ul><li>`msgpack`：MessagePack 编码（默认）。1M+ 长上下文查询下，请求体积缩减约 55%，端到端查询耗时（含客户端序列化、服务端哈希/匹配/序列化、网络传输）约为 JSON 的 1/4（5M 上下文约 42ms vs 220ms）。</li><li>`json`：传统 JSON 编码，用于对接旧版本 KV Conductor。</li></ul>**滚动升级/混部注意**：请求侧无自动降级——须先升级 kv-conductor 再升级 Coordinator；若混部（新版 Coordinator + 旧版 conductor，或反之），须显式配置 `query_encoding: "json"` 直至两端同版本。|
| block_size |int|KV Cache块大小，默认值：128。DeepSeek V4 须设为 512，并与引擎 `--block-size` 保持一致。|
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
| probe_max_attempts |int|发现精度异常后会进行拔测的次数， 默认值：3。|
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
| additional_annotations | object | 可选；Coordinator工作负载及其Pod模板的自定义Annotations。 |
| additional_labels | object | 可选；Coordinator工作负载及其Pod模板的自定义Labels。 |

## motor_engine_union_config

motor_engine_union_config字段用于**PD混部场景**，配置同一类 union 原生引擎实例。其结构与motor_engine_prefill_config/motor_engine_decode_config类似，但不区分P/D两套引擎配置，也无需配置 kv_transfer_config的producer/consumer角色。其配置样例如下所示。

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
    "enforce-eager": false,
    "max_model_len": 2048,
    "kv_transfer_config": {
      "kv_connector": "MooncakeConnectorV1",
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
      "service_ports": []
    },
    "basic_config": {...
    },
    "snapshot_config": {
      "enable_snapshot": false,
      "snapshot_metadata_path": ""
    },
    "vllm_startup_acceleration_config": {
      "enable_startup_plan": true,
      "enable_graph_reuse": true,
      "cache_root": "/mnt/vllm-cache/union"
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
    "fault_tolerance_config": {
      "enable_fault_tolerance": false,
      "poll_interval_sec": 5.0,
      "poll_timeout_sec": 5.0,
      "max_poll_failures": 3
    },
    "port_allocator_config": {
      "enable": true,
      "scan_range": 100,
      "probe_timeout_seconds": 0.5,
      "remote_check_timeout_seconds": 1.0,
      "bind_host": "0.0.0.0"
    }
  },
  "additional_annotations": {
    "motor-component": "union-engine"
  },
  "additional_labels": {
    "motor-component": "union-engine"
  }
}
```

**表4** <a id="motor_nodemanger_config"></a>motor_engine_union_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| engine_type | string | 引擎类型，如 `vllm` |
| additional_annotations | object | 可选；Union Engine工作负载及其Pod模板的自定义Annotations。 |
| additional_labels | object | 可选；Union Engine工作负载及其Pod模板的自定义Labels。 |
| **engine_config字段** | - | `engine_config` 直接映射所选引擎的原生启动参数；请参阅对应 vLLM/SGLang 版本的官方参数文档。 |
| **motor_nodemanger_config字段** |-|-|
| api_config.pod_ip |string | Pod IP（由环境或部署注入）。默认值：`127.0.0.1`（或 Env.pod_ip） |
| api_config.node_manager_port |int | NodeManager 端口。默认值：`1026` |
| endpoint_config.endpoint_num |int | 引擎端点数量，通常由 HCCL/并行配置推导。默认值：`0` |
| endpoint_config.base_port |int | 端点端口起始号。默认值：`10000` |
| endpoint_config.service_ports |array | 各端点推理服务端口列表（整数数组）。默认值：`[]` |
| endpoint_config.bootstrap_port |int/null | SGLang PD 原生 bootstrap 端口。由所选引擎配置中的 `engine_config.disaggregation_bootstrap_port`（兼容 `disaggregation-bootstrap-port`）派生；vLLM 或未配置时为空。 |
| fault_tolerance_config.enable_fault_tolerance |bool|是否显式开启引擎软件故障轮询，默认值：false。<br>引擎 user config 检测到 FT 开关时自动开启，无需显式配置。|
| fault_tolerance_config.poll_interval_sec |float|轮询引擎 FT 状态的时间间隔，单位：秒，默认值：5.0。|
| fault_tolerance_config.poll_timeout_sec |float|单次轮询的 HTTP 超时，单位：秒，默认值：5.0。|
| fault_tolerance_config.max_poll_failures |int|连续轮询失败阈值，达到后按 dead 上报，默认值：3。|
| snapshot_config.enable_snapshot |bool|是否使能容器快照功能总开关，默认值：false。<br>开启后，用户可对实例容器制作快照镜像，并支持由快照恢复的实例向控制面注册。|
| snapshot_config.snapshot_metadata_path |string|容器快照元数据文件路径，包含容器快照制作与恢复过程中依赖的元数据，默认值为空。|
| vllm_startup_acceleration_config.enable_startup_plan |bool|是否启用 vLLM StartPlan，默认值：false。命中已有 StartPlan 时由 vLLM 跳过 memory profiling。|
| vllm_startup_acceleration_config.enable_graph_reuse |bool|是否启用 vLLM-Ascend 后端完整图复用，默认值：false。|
| vllm_startup_acceleration_config.cache_root |string|可选的 vLLM StartPlan 与编译缓存绝对路径。缺省时继承 `VLLM_CACHE_ROOT`，再缺省时使用 vLLM 默认目录；生产环境建议显式配置持久化路径。|
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
| **kv_cache_store_config字段** |-|-|
| kv_cache_store_config | object | KV 池化配置（`enable`/`backend`/`store_mode`/`global_segment_size`/`local_buffer_size`/`store_http_port` 等），未配置则不启用池化。完整字段与默认值见 [KV 池化 README — kv_cache_store_config](../features/kv_cache_store/README.md#kv_cache_store_config全局配置)；Mooncake `standalone` 部署模式说明见 [Mooncake 后端文档](../features/kv_cache_store/backend/mooncake.md#standalone-模式独立-store-进程)。 |

## motor_engine_prefill_config/motor_engine_decode_config

motor_engine_prefill_config和motor_engine_decode_config字段用于**PD分离部署场景**，这两个字段分别配置Prefill与Decode引擎。两者结构相同，均需指定engine_type与engine_config；`health_check_config` 用于配置原生 `/health` 超时与模型加载启动窗口。配置示例如下所示。

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
    "enforce-eager": false,
    "max_model_len": 2048,
    "kv_transfer_config": {
      "kv_connector": "MooncakeConnectorV1",
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
      "service_ports": []
    },
    "basic_config": {...
    },
    "snapshot_config": {
      "enable_snapshot": false,
      "snapshot_metadata_path": ""
    },
    "vllm_startup_acceleration_config": {
      "enable_startup_plan": true,
      "enable_graph_reuse": true,
      "cache_root": "/mnt/vllm-cache/prefill"
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
    "fault_tolerance_config": {
      "enable_fault_tolerance": false,
      "poll_interval_sec": 5.0,
      "poll_timeout_sec": 5.0,
      "max_poll_failures": 3
    },
    "port_allocator_config": {
      "enable": true,
      "scan_range": 100,
      "probe_timeout_seconds": 0.5,
      "remote_check_timeout_seconds": 1.0,
      "bind_host": "0.0.0.0"
    }
  },
  "additional_annotations": {
    "motor-component": "prefill-engine"
  },
  "additional_labels": {
    "motor-component": "prefill-engine"
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
    "enforce-eager": false,
    "max_model_len": 2048,
    "kv_transfer_config": {
      "kv_connector": "MooncakeConnectorV1",
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
      "service_ports": []
    },
    "basic_config": {...
    },
    "snapshot_config": {
      "enable_snapshot": false,
      "snapshot_metadata_path": ""
    },
    "vllm_startup_acceleration_config": {
      "enable_startup_plan": true,
      "enable_graph_reuse": true,
      "cache_root": "/mnt/vllm-cache/decode"
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
    "fault_tolerance_config": {
      "enable_fault_tolerance": false,
      "poll_interval_sec": 5.0,
      "poll_timeout_sec": 5.0,
      "max_poll_failures": 3
    },
    "port_allocator_config": {
      "enable": true,
      "scan_range": 100,
      "probe_timeout_seconds": 0.5,
      "remote_check_timeout_seconds": 1.0,
      "bind_host": "0.0.0.0"
    }
  },
  "additional_annotations": {
    "motor-component": "decode-engine"
  },
  "additional_labels": {
    "motor-component": "decode-engine"
  }
}

```

**表5** motor_engine_prefill_config/motor_engine_decode_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------------------|
| engine_type | string | 引擎类型，如 `vllm` |
| additional_annotations | object | 可选；Prefill/Decode Engine工作负载及其Pod模板的自定义Annotations。 |
| additional_labels | object | 可选；Prefill/Decode Engine工作负载及其Pod模板的自定义Labels。 |
| **engine_config字段** | - | `engine_config` 直接映射所选引擎的原生启动参数；请参阅对应 vLLM/SGLang 版本的官方参数文档。 |
| **motor_nodemanger_config字段** |-|-|
| api_config.pod_ip |string | Pod IP（由环境或部署注入）。默认值：`127.0.0.1`（或 Env.pod_ip） |
| api_config.node_manager_port |int | NodeManager 端口。默认值：`1026` |
| endpoint_config.endpoint_num |int | 引擎端点数量，通常由 HCCL/并行配置推导。默认值：`0` |
| endpoint_config.base_port |int | 端点端口起始号。默认值：`10000` |
| endpoint_config.service_ports |array | 各端点推理服务端口列表（整数数组）。默认值：`[]` |
| endpoint_config.bootstrap_port |int/null | SGLang PD 原生 bootstrap 端口。由所选引擎配置中的 `engine_config.disaggregation_bootstrap_port`（兼容 `disaggregation-bootstrap-port`）派生；vLLM 或未配置时为空。 |
| fault_tolerance_config.enable_fault_tolerance |bool|是否显式开启引擎软件故障轮询，默认值：false。<br>引擎 user config 检测到 FT 开关时自动开启，无需显式配置。|
| fault_tolerance_config.poll_interval_sec |float|轮询引擎 FT 状态的时间间隔，单位：秒，默认值：5.0。|
| fault_tolerance_config.poll_timeout_sec |float|单次轮询的 HTTP 超时，单位：秒，默认值：5.0。|
| fault_tolerance_config.max_poll_failures |int|连续轮询失败阈值，达到后按 dead 上报，默认值：3。|
| snapshot_config.enable_snapshot |bool|是否使能容器快照功能总开关，默认值：false。<br>开启后，用户可对实例容器制作快照镜像，并支持由快照恢复的实例向控制面注册。|
| snapshot_config.snapshot_metadata_path |string|容器快照元数据文件路径，包含容器快照制作与恢复过程中依赖的元数据，默认值为空。|
| vllm_startup_acceleration_config.enable_startup_plan |bool|是否启用 vLLM StartPlan，默认值：false。命中已有 StartPlan 时由 vLLM 跳过 memory profiling。|
| vllm_startup_acceleration_config.enable_graph_reuse |bool|是否启用 vLLM-Ascend 后端完整图复用，默认值：false。|
| vllm_startup_acceleration_config.cache_root |string|可选的 vLLM StartPlan 与编译缓存绝对路径。缺省时继承 `VLLM_CACHE_ROOT`，再缺省时使用 vLLM 默认目录；生产环境建议显式配置持久化路径。|
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
| **kv_cache_store_config字段** |-|-|
| kv_cache_store_config | object | KV 池化配置（`enable`/`backend`/`store_mode`/`global_segment_size`/`local_buffer_size`/`store_http_port` 等），未配置则不启用池化。完整字段与默认值见 [KV 池化 README — kv_cache_store_config](../features/kv_cache_store/README.md#kv_cache_store_config全局配置)；Mooncake `standalone` 部署模式说明见 [Mooncake 后端文档](../features/kv_cache_store/backend/mooncake.md#standalone-模式独立-store-进程)。 |

PD模式下P与D**各自独立配置**"health_check_config"，未配置时使用代码默认值。

### vLLM 启动加速

`vllm_startup_acceleration_config` 仅适用于 `engine_type` 为 `vllm` 的引擎配置。

- `enable_startup_plan=true`：启用 vLLM StartPlan。Profile 命中并通过安全检查后跳过 memory profiling。
- `enable_startup_plan=false`：不生成或加载 StartPlan。
- `enable_startup_plan=true` 需要配套的 vLLM/vLLM-Ascend 版本已经接入 StartPlan；Motor 不修改运行时源码。
- `enable_graph_reuse=true`：启用 vLLM-Ascend 后端完整图复用，并以该配置为准设置
  `enable_npugraph_ex=true`、`enforce_eager=false` 和角色对应的 `cudagraph_mode`。
- `enable_graph_reuse=false`：关闭 vLLM-Ascend 后端完整图复用，不强制关闭普通图捕获或 AOT 缓存。
- `cache_root`：vLLM 缓存根目录，用于保存 StartPlan、AOT 和后端编译图缓存。PD 分离场景建议为 Prefill、
  Decode 配置各自可持久化且对 vLLM 进程可读写的目录。

### dispatch_profile

当 `engine_config.kv_transfer_config.kv_connector` 不在内置识别白名单内时，可在 `motor_engine_prefill_config` / `motor_engine_decode_config` 顶层显式声明 P/D 协同语义。NodeManager 根据该字段推导兼容元数据，并在构造原生 vLLM 启动命令时校验其语义。

| 配置项 | 类型 | 说明 |
|--------|------|------|
| dispatch_profile | string | 可选值为 `handoff` 或 `trigger`。原生 vLLM P/D 同时接受二者：`handoff` 为 Prefill 完成后交给 Decode；`trigger` 为 Decode 先启动并经 Worker metaserver 触发 Prefill（`MooncakeLayerwiseConnector` 自动推导为 `trigger`）。SGLang 使用自身 bootstrap 协议。Prefill 与 Decode 两端应保持一致。 |

>[!NOTE]说明
> `dispatch_profile` 写在 `motor_engine_*_config` 顶层，不是在 `engine_config` 内部。`dispatch_capabilities` 为内部兼容字段，不支持用户直接填写。

### health_check_config

可选虚推（虚拟推理）健康探测配置，位于 `motor_engine_prefill_config` / `motor_engine_decode_config` 模块，默认关闭，机制说明见 [虚推健康探测](../features/sim_inference.md)。

**表7** health_check_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------|
| enable_virtual_inference | bool | Motor 主动虚推开关，默认值：false。取值为 `true` 时，在推理面 `/health` 首次 READY 后启动周期性虚推。仅对 vLLM 的 DP rank 0、非 headless endpoint 生效；设为 `false` 不会关闭 SGLang 原生生成式 `GET /health`。仅允许在最终引擎环境 `ASCEND_GLOBAL_LOG_LEVEL=3`（未配置时默认为 ERROR）时开启。 |
| npu_usage_threshold | int | AI Cube 利用率阈值（%），默认值：3。仅 vLLM Motor 虚推使用，且须满足 `0 < npu_usage_threshold <= 100`。 |
| max_failure_count | int | 连续虚推失败次数上限，默认值：6。仅 vLLM Motor 虚推使用；达到阈值后 endpoint 降级为 ABNORMAL，但不触发进程重启。 |
| virtual_inference_timeout | float | 周期性主动虚推请求超时（秒），默认值：5.0，必须为正数。仅对 vLLM Motor 虚推生效；首次 warmup 请求固定为 180 秒。 |
| health_collector_timeout | int | 推理面 `GET /health` 探测超时（秒），默认值：5。vLLM 与 SGLang 心跳均使用。 |
| health_collector_timeout_retry_attempts | int | 单次 `GET /health` 超时后的最大尝试次数（包含首次请求），默认值：3；仅超时重试。 |
| startup_timeout | int | 原生引擎模型加载启动窗口（秒），默认值：1800。窗口内连接失败保持 STARTING，不判定实例异常。 |

## 其他参数说明

### motor_engine_union_env字段

PD混部场景下，union 原生引擎的环境变量配置在 `env.json` 的 `motor_engine_union_env` 中。示例可参考 `examples/infer_engines/vllm/pd_hybrid/env.json`。

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
Coordinator 会根据实例角色自动识别 P/D 分离或 union 混部拓扑，并根据 `engine_type` 选择 vLLM handoff 或 SGLang bootstrap Adapter。vLLM Connector 白名单、`MultiConnector` 取 `connectors[0]` 的规则，以及未知 Connector 在启动期 fail-closed 的处理，详情请参见[PD 分离特性说明](../../design/pd_disaggregation.md#vllm-connector-识别白名单)与[PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md)。

**表9** prefill_kv_event_config说明

| 来源 | 说明 |
|------|------|
| PD 分离 | 从 `motor_engine_prefill_config.engine_config.kv-events-config` 推导 |
| PD 混部 | 从 `motor_engine_union_config.engine_config.kv-events-config` 推导 |
| kv_conductor_config | `http_server_port` 写入 `prefill_kv_event_config.http_server_port`；未配置时默认 `13333` |
