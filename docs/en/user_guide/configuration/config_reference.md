# user_config.json Configuration File Full Parameter Description

This document describes in detail all configurable items of components such as Controller and Coordinator in the `user_config.json` configuration file, whose structure corresponds to the structure of `examples/features/config_sample.json`. During deployment, the system merges the corresponding modules in `user_config.json` into the component runtime configuration, following the principle of "code defaults take precedence unless overridden by user configuration". In addition, dynamic activation is supported by modifying the configuration file monitored by the component. The configuration file is located in the `examples/infer_engines/` directory (for example, `examples/infer_engines/vllm/user_config.json`). Select the corresponding configuration based on the engine type and model actually used.

## motor_deploy_config

The `motor_deploy_config` field contains deployment- and resource-related configurations. It is read by `deploy.py` and used to generate K8s resources, inject environment variables, and so on. Its configuration example is as follows:

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

**Table 1** `motor_deploy_config` field parameter description

| Configuration Item | Type | Description |
|--------|------|------|
| p_instances_num | int | Number of P instances. Value range: [1,16] |
| d_instances_num | int | Number of D instances. Value range: [1,16] |
| single_p_instance_pod_num | int | Number of Pods corresponding to a single P instance. Value range: greater than or equal to 1 |
| single_d_instance_pod_num | int | Number of Pods corresponding to a single D instance. Value range: greater than or equal to 1 |
| p_pod_npu_num | int | Number of NPU cards occupied by a single P instance Pod. Each Pod supports a maximum of 16 cards |
| d_pod_npu_num | int | Number of NPU cards occupied by a single D instance Pod. Each Pod supports a maximum of 16 cards |
| image_name | string | Inference image name (must include the runtime environment such as MindIE Motor and vLLM) |
| job_id | string | Deployment task name, also used as the K8s namespace, for example, "mindie-motor" |
| hardware_type | string | Hardware type: <ul><li>Atlas 800I A2 inference server: 800I_A2</li><li>Atlas 800I A3 SuperPoD server: 800I_A3</li><li>Atlas 850 Server: 850-Atlas-8p-8</li><li>Atlas 850 SuperPoD server: 850-SuperPod-Atlas-8</li></ul>|
| weight_mount_path | string | Model weight mount path on the host machine. The model_path in the container must be consistent with this mount path, for example, `"/mnt/weight/"` |
| tls_config | object | Optional; TLS-related configuration, including five types: `mgmt_tls_config`, `infer_tls_config`, `etcd_tls_config`, `grpc_tls_config`, and `observability_tls_config` |

## motor_controller_config

A configuration example of the `motor_controller_config` field is as follows:

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

**Table 2** `motor_controller_config` field parameter description

| Configuration Item | Type | Description |
|--------|------|------------------|
| **logging_config field** |-|-|
| log_level | string | Log level, default value: `INFO`.<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul> |
| log_max_line_length | int | Maximum length of a single log entry, truncate if exceeded. Default value: `8192`. |
| log_format | string | Log format template, supports Python logging placeholders. Default value: `"(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s"`. |
| log_date_format | string | Log date format, default value: `"%m-%d %H:%M:%S"`. |
| host_log_dir| string | Log storage path, default value: `"/root/ascend/log"`. |
| log_rotation_size | int | Log dump file size, default value: `20`. |
| log_rotation_count | int |Log dump file count, default value: `10`.|
| log_compress |bool| Whether to enable log compression, default value: `false`. |
| log_compress_level |int|Log compression level, default value: `6`.|
| log_max_total_size |int|Total log file size, unit: MB, default value: `200`.|
| log_cleanup_interval |int|Log cleanup interval, unit: seconds, default value: `1800`.|
| third_party_log_levels |string|Third-party log level, default value: `WARNING`.<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul>|
| **api_config field** |-|-|
| controller_api_host | string | Controller API listening address (IP or hostname), default value: `127.0.0.1` (or Env.pod_ip). |
| controller_api_dns |string|Controller API domain name, default value: "mindie-motor-controller-service.mindie-motor.svc.cluster.local".|
| controller_api_port | int | Controller API port, default value: `1026`. |
| observability_api_port |int|Controller observability API port, default value: `1027`.|
| **instance_config field** |-|-|
| instance_assemble_timeout | int | Maximum waiting time for instances to become ready, unit: seconds, default value: `600`. |
| instance_assembler_check_interval | int | Interval for polling the instance assembly status, unit: seconds, default value: `1`. |
| instance_assembler_cmd_send_interval | int | Interval for sending assembly commands to instances, unit: seconds, default value: `1`. |
| instance_manager_check_interval | int | Instance status inspection interval, unit: seconds, default value: `1`. |
| instance_heartbeat_timeout | int | If no instance heartbeat is received within this duration, the instance is determined to be abnormal, unit: seconds, default value: `10`. |
| instance_expired_timeout | int | If an instance remains idle beyond this duration, it is cleaned up, unit: seconds, default value: `1200`. |
| send_cmd_retry_times | int | Number of retries when sending a command to an instance fails, default value: `3`. |
| **event_config field** |-|-|
| event_consumer_sleep_interval | float | Event queue polling interval, that is, the waiting time after each event is processed, unit: seconds, default value: `1.0`. |
| coordinator_heartbeat_interval | float | Heartbeat reporting interval between the Controller and the Coordinator, unit: seconds, default value: `10.0`. |
|<a id="fault_tolerance_config"></a>**fault_tolerance_config field**|-|-|
| enable_fault_tolerance | bool | Whether to enable fault self-healing (advanced RAS), default value: true. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul> |
| strategy_center_check_interval | int | Strategy center polling interval, unit: seconds, default value: `1`. |
| configmap_namespace |string|configmap namespace, default value: "kube-system".|
| configmap_prefix |string|configmap prefix, default value: "mindx-dl-deviceinfo-".|
| k8s_cert_path |string|Security certificate path, empty by default.|
| enable_scale_p2d | bool | Whether to enable ScaleP2D elastic scaling, default value: false. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul> |
| enable_token_reinference | bool | Whether to enable Token Reinference fault recovery, default value: true. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul> |
| scale_p2d_d_instance_reinit_wait_timeout |int|Maximum time to wait for the D instance to self-recover (reinitialize) before ScaleP2D performs preemption, unit: seconds, default value: `60`.<br>During the waiting period, if the D instance recovers to initial/active, ScaleP2D is no longer executed; after timeout, if the D instance is still in a preemptible state such as inactive, the subsequent P instance selection process continues.|
| **observability_config field**|-|-|
| observability_enable |bool|Whether to enable observability, default value: false. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul>|
| metrics_ttl |int|metrics query interval, unit: seconds, default value: `5`.|
| **standby_config field**|-|-|
| enable_master_standby | bool | Whether to enable Controller master/standby. Options: `true` / `false`. Default value: `false` |
| master_standby_check_interval | int | Primary/standby role probe interval (seconds). Default value: `5` |
| master_lock_ttl | int | Lease duration for the primary node to hold the lock on ETCD (seconds). Default value: `10` |
| master_lock_retry_interval | int | Retry interval for acquiring the lock when contending for primary (seconds). Default value: `5` |
| master_lock_max_failures | int | If consecutive master contention failures exceed this count, give up and switch over. Default value: `3` |
| master_lock_key | string | Lock path of the primary node in ETCD; the prefix `/controller/` is automatically added at runtime. Default value: `/master_lock` (actually `/controller/master_lock`) |
| **etcd_config field** |-|-|
| etcd_host | string | ETCD service address (hostname or IP). Default value: `etcd.default.svc.cluster.local` |
| etcd_port | int | ETCD port. Default value: `2379` |
| etcd_timeout | int | ETCD operation timeout duration (seconds). Default value: `5` |
|etcd_lb_policy|string|ETCD load balancing policy, default value: `round_robin`.|
| enable_etcd_persistence | bool | Whether to enable ETCD persistence. Options: `true`/`false`. Default value: `false` |
| **port_allocator_config field** |-|-|
| enable |bool|Whether to enable automatic port allocation, default value: `true`.|
| scan_range |int|Port scanning range, default value: `100.|
| probe_timeout_seconds |float|Probe timeout duration, default value: `0.5`.|
| remote_check_timeout_seconds |float|Remote detection timeout duration, default value: `1.0`.|
| bind_host |string|Host address to be bound, default value: `0.0.0.0`.|

## motor_coordinator_config

The following shows a configuration example of the `motor_coordinator_config` field:

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
  "worker_index": null
}
```

**Table 3** `motor_coordinator_config` field parameter description

| Configuration Item | Type | Description |
|--------|------|------------------|
| context_budget_mode | string | Before request routing, trim `max_tokens`/`max_completion_tokens` to the model's remaining context based on the prompt token count. Optional values: `off` (default) or `on`; when `on` is enabled, the P/D engine configuration must provide `model` and `max_model_len`. |
| log_level | string | Log level. Default value: `INFO`<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul> |
| log_max_line_length | int | Maximum length of a single log line; lines exceeding this limit are truncated. Default value: `8192`. |
| log_format | string | Log format template, supporting Python logging placeholders. Default value: `"(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s"`. |
| log_date_format | string | Log date format. Default value: `"%m-%d %H:%M:%S"`. |
| host_log_dir| string | Log storage path. Default value: `"/root/ascend/log"`. |
| log_rotation_size | int | Log dump file size. Default value: `20`. |
| log_rotation_count | int |Log dump file count. Default value: `10`.|
| log_compress |bool| Whether to enable log compression. Default value: `false`. |
| log_compress_level |int|Log compression level. Default value: `6`.|
| log_max_total_size |int|Total log file size, in MB. Default value: `200`.|
| log_cleanup_interval |int|Log cleanup interval, in seconds. Default value: `1800`.|
| third_party_log_levels |string|Third-party log level. Default value: `WARNING`.<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul>|
| **prometheus_metrics_config field** |-|-|
| reuse_time | int | Background collection period, in seconds. Default value: 3. |
| enable_kv_store_metrics |bool|Whether to pull metrics from the KV pooling backend (MemCache/Mooncake). Automatically enabled when `kv_cache_store_config` is configured, with no manual intervention required; this field is used only when you need to explicitly override the automatic behavior. Default value: `false`.|
| kv_store_metrics_endpoint |string|URL of the KV pooling metrics. Automatically concatenated when `kv_cache_store_config` is configured (`http://{KVS_MASTER_SERVICE}:{KV_CACHE_STORE_PORT}/metrics`), with no manual configuration required; this field is used only when you need to explicitly override the auto-generated URL. Empty by default.|
| **exception_config field** |-|-|
| max_retry | int | Maximum number of retries after a request fails. Default: `5`. |
| transport_max_retry | int/null | Maximum number of attempts for Coordinator transport failures; when `null`, `max_retry` is used. Default: `null`. |
| retry_delay | float | Wait time before each retry (seconds). Default: `0.2`. |
| first_token_timeout | int | Timeout duration for waiting for the first token to be returned (seconds). Default: `600`. |
| infer_timeout | int | Total timeout duration for a single inference request (seconds). Default: `3600`. |
| upstream_error_body_max_bytes | int | Maximum number of bytes of the engine HTTP error body to pass through to the client, to avoid returning oversized error responses. Default: `65536`. |
| **reschedule_config field** |-|-|
| enable | bool | Switch for the rescheduling feature in fault scenarios. Default: `false`.<br>Model recomputation is handled by the engine side, and this configuration does not control engine-side recomputation; `recompute_enabled` is only a legacy compatibility alias for `reschedule_enabled`; `recompute_max_retry` has been removed and will be ignored. |
| **scheduler_config field** |-|-|
| scheduler_type | string | Scheduling type. Default value: `load_balance`<ul><li>`load_balance`: load balancing;</li><li>`round_robin`: round robin;</li><li>`kv_cache_affinity`: KV Cache affinity scheduling.</li></ul> |
| enable_pd_separation_fallback_to_hybrid | bool | In PD disaggregation scenarios, when D instances are unavailable or P/D instances do not meet scheduling conditions, whether to allow fallback to hybrid routing. Default value: `true`. |
| endpoint_instance_score_weight | float | Weight of the average instance load when endpoint prioritizes load balancing. Default value: `0.05`. |
| kv_affinity_mode | string | Sub-policy when `scheduler_type=kv_cache_affinity`: `unified` (default) or `load_gated`. |
| kv_affinity_load_weight | float | Weight of the endpoint real-time load in unified mode. Default value: `1.0`. |
| kv_affinity_overlap_credit | float | Discount factor of the cache prefix on prefill cost. Default value: `1.0`. |
| kv_affinity_prefill_load_scale | float | Weight of the prefill cost (after affinity discount) in unified mode. Default value: `1.0`. |
| kv_affinity_load_gate_topn | int | In load_gated mode, first retain the N endpoints with the lowest load and then perform affinity-based selection; when `0`, fall back to `2`. Default value: `0`. |
| **inference_workers_config field** |-|-|
| num_workers | int | Number of business-plane workers in the Coordinator. Default value: `4`. |
| **timeout_config field** |-|-|
| request_timeout | int | Timeout duration for a single HTTP request (seconds). Default value: `30` |
| connection_timeout | int | Timeout duration for establishing a connection (seconds). Default value: `10` |
| read_timeout | int | Timeout duration for read operations (seconds). Default value: `15` |
| write_timeout | int | Timeout duration for write operations (seconds). Default value: `15` |
| keep_alive_timeout | int | Connection keep-alive duration; the connection is closed if no activity occurs within the timeout (seconds). Default value: `60`. |
| **api_key_config field** |-|-|
| enable_api_key | bool | Whether to enable API Key authentication. Optional: `true`/`false`. Default value: `false`. |
| valid_keys | array | List of valid API Key strings. Default value: `[]`. |
| header_name | string | Name of the HTTP header carrying the API Key. Default value: `Authorization`. |
| key_prefix | string | Prefix of the Key in the header, such as `Bearer`. Default value: `Bearer`. |
| skip_paths | array | List of paths that do not require API Key validation (such as `/metrics`, `/liveness`, `/docs`, etc.), customizable |
| encryption_algorithm | string | Encryption algorithm used for Key validation, such as `PBKDF2_SHA256`. Default value: `PBKDF2_SHA256`. |
| **rate_limit_config field** |-|-|
| enable_rate_limit | bool | Whether to enable request rate limiting. Optional: `true`/`false`. Default value: `false`. |
| provider |string|Rate limiting provider. simple uses the built-in token bucket; OLC uses the overload control library (requires additional installation and configuration).|
| max_requests | int | Maximum number of requests allowed within the rate limiting time window. Default value: `1000`. |
| window_size | int | Length of the time window for rate limiting statistics (seconds). Default value: `60`. |
| scope | string | Scope of rate limiting, such as `global`. Default value: `global`. |
| skip_paths | array | List of paths that do not participate in rate limiting statistics (such as `/liveness`, `/readiness`, `/metrics`), customizable. |
| error_message | string | Message returned to the client when rate limiting is triggered. Default: `too many requests, please try again later`. |
| error_status_code | int | HTTP status code returned when rate limiting is triggered, usually 4xx (such as 429). Default: `429`. |
| max_request_body_size | float | Maximum request body size (MB); requests exceeding this are rejected directly with 413 and do not consume rate limiting tokens. `= 0` means no limit. Decimal values are supported (such as `0.5` for 0.5MB, 1MB = 1024\*1024 bytes). Default: `0` (no limit). |
| olc_config_path |string|Absolute path of the OLC rule configuration directory, or a relative path to the service startup directory. The directory must contain overload-config.properties and olc.json.|
| **standby_config field** |-|-|
| enable_master_standby | bool | Whether to enable Coordinator primary/standby. Optional: `true` / `false`. Default value: `false`. |
| master_standby_check_interval | int | Interval for primary/standby role probing (seconds). Default value: `5`. |
| master_lock_ttl | int | Lease duration for the primary node to hold the lock on ETCD (seconds). Default value: `10`. |
| master_lock_retry_interval | int | Retry interval for acquiring the lock when competing for primary (seconds). Default value: `5`. |
| master_lock_max_failures | int | If consecutive failures to acquire primary exceed this count, give up and switch over. Default value: `3`. |
| master_lock_key | string | Lock path of the primary node in ETCD; the prefix `/coordinator/` is automatically added at runtime. Default value: `/master_lock` (actually `/coordinator/master_lock`). |
| **etcd_config field** |-|-|
| etcd_host | string |ETCD service address (hostname or IP). Default value: `etcd.default.svc.cluster.local`. |
| etcd_port | int | ETCD port. Default value: `2379`. |
| etcd_timeout | int | ETCD operation timeout duration (seconds). Default value: `5`. |
|etcd_lb_policy|string|ETCD load balancing policy. Default value: round_robin.
| enable_etcd_persistence | bool | Whether to enable ETCD persistence. Optional: `true` / `false`. Default value: `false`. |
| **aigw_model field** |-|This parameter is the centralized configuration of AIGW model metadata, used for model information returned by interfaces such as /v1/models. In user_config.json, it corresponds to the aigw object under motor_coordinator_config; it is null when not used, and its internal configurable items are as follows.|
| id | string | Model ID, consistent with the model name in OpenAI-compatible interfaces. If the model_name of Prefill/Decode is configured, it is auto-filled with the Prefill model_name during deployment |
| object | string | Object type, fixed to `model`. Auto-filled during deployment when not configured. |
| owned_by | string | Model ownership identifier, such as `motor`. Auto-filled during deployment when not configured. |
| p_max_seqlen | int | Maximum sequence length on the Prefill side (positive integer). When not configured, auto-filled from the Prefill `engine_config.max_model_len`. |
| d_max_seqlen | int | Maximum sequence length on the Decode side (positive integer). When not configured, auto-filled from the Decode `engine_config.max_model_len`. |
| slo_ttft | int | First-token latency SLO (milliseconds), used for scheduling/monitoring. Default value: `1000`. |
| slo_tpot | int | Per-token latency SLO (milliseconds), used for scheduling/monitoring. Default value: `50`. |
| **api_config field** |-|-|
| coordinator_api_host | string | Coordinator API listening address (IP or hostname). Default value: `127.0.0.1` (or Env.pod_ip). |
| coordinator_api_dns | string | Coordinator management-plane API domain name. Default value: mindie-motor-coordinator-service.mindie-motor.svc.cluster.local. |
| coordinator_api_infer_dns | string | Coordinator business-plane API domain name. Default value: mindie-motor-coordinator-service.mindie-motor.svc.cluster.local. |
| coordinator_api_obs_dns | string | Coordinator observability API domain name. Default value: mindie-motor-coordinator-service.mindie-motor.svc.cluster.local. |
| coordinator_api_infer_port | int | Inference-plane port. Default value: `1025`. |
| coordinator_api_mgmt_port | int | Management-plane port. Default value: `1026`. |
| coordinator_obs_port | int | Observability port, hosting observability interfaces such as `/metrics`. Default value: `1027`. |
| **tracer_config field** |-|-|
| endpoint |string|Reporting address of the tracing data or the access point of the backend service. Empty by default.|
| root_sampling_rate |float|Root sampling rate, the sampling probability of tracing data that has no parent Span (i.e., the entry point of a request, such as the first entry of an HTTP request). Default value: `1.0`, indicating that all new root requests are recorded. If set to `0.5`, only half of the new requests are recorded, and the other half are discarded.|
| remote_parent_sampled |float|Remote parent sampling rate (when the parent Span is sampled), the sampling probability of the current Span when its parent Span comes from another service (remote call) and the remote parent Span has already been sampled. Default value: `1.0`, indicating that the current call is 100% recorded.|
| remote_parent_not_sampled |float|Remote parent sampling rate (when the parent Span is not sampled), the sampling probability of the current Span when its parent Span comes from another service (remote call) but the remote parent Span has not been sampled. Default value: `1.0`, indicating that the current call is 100% recorded.|
| local_parent_sampled |float|Local parent sampling rate (when the parent Span is sampled), the sampling probability of the current Span when its parent Span comes from within the same service instance (local call) and the parent Span has already been sampled. Default value: `1.0`, indicating that the current call is 100% recorded.|
| local_parent_not_sampled |float|Local parent sampling rate (when the parent Span is not sampled), the sampling probability of the current Span when its parent Span comes from within the same service instance (local call) but the parent Span has not been sampled. Default value: `1.0`, indicating that the current call is 100% recorded.|
| **prefill_kv_event_config field** |-|-|
| conductor_service |string|Conductor service IP or domain name. Empty by default.|
| http_server_port |int|HTTP service port of the KV Conductor. Default value: `13333`, value range: [1024,65535].|
| block_size |int|KV Cache block size. Default value: `128`.|
| endpoint |string|Endpoint for P instance event publishing. Empty by default. Example value: `tcp://*:\<port>`.|
| replay_endpoint |string|Event replay endpoint. Empty by default. Example value: `tcp://*:\<port>`.|
| engine_type |string|Engine type. Default value: `vLLM`.|
| model_path |string|Model weight path. Empty by default.|
|re_register_interval_sec|int|Re-registration interval. Default value: `0`.|
| **token_sampling_config field** |-|-|
| interval_seconds |float|Interval between each sampling. Default value: `30.0`.|
| logprobs_count |int|Number of log_prob values to return during sampling. Default value: `1`. Values are as follows:<ul><li>`1`: can only detect repetition.</li><li>`3`: can detect repetition and garbled text.</li><li>`5`: can detect repetition, garbled text, and rare characters.</li></ul>|
| precision_check_enabled |bool|Whether to enable precision anomaly detection. Default value: `false`.|
| precision_issue_threshold |int|Number of consecutive anomalies that will be determined as a precision anomaly and trigger reporting. Default value: `10`.|
| probe_max_attempts |int|Number of probe tests performed after a precision anomaly is detected. Default value: `3`.|
| probe_timeout_seconds |float|Timeout duration for a single probe test. Default value: `600.0`.|
| **port_allocator_config field** |-|-|
| enable |bool|Whether to enable automatic port allocation. Default value: `true`.|
| scan_range |int|Port scanning range. Default value: `100`.|
| probe_timeout_seconds |float|Probe timeout duration. Default value: `0.5`.|
| remote_check_timeout_seconds |float|Remote detection timeout duration. Default value: `1.0`.|
| bind_host |string|Host address to be bound. Default value: `0.0.0.0`.|
| **request_limit field** |-|This field is not included in `config_sample.json`, but is commonly used in PD deployment and takes effect after being merged into the runtime configuration.|
| single_node_max_requests | int | Maximum number of concurrent requests allowed on a single node, configured by `user_config`. |
| max_requests | int | Maximum number of concurrent requests globally in the cluster, configured by `user_config`. |

## motor_engine_union_config

The `motor_engine_union_config` field is used in the **PD co-location scenario** to configure the same type of union Engine Server instances. Its structure is similar to `motor_engine_prefill_config`/`motor_engine_decode_config`, but it does not distinguish between the P/D engine configurations, nor does it require configuring the producer/consumer roles of `kv_transfer_config`. Its configuration example is as follows.

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

**Table 4** <a id="motor_nodemanger_config"></a>motor_engine_union_config field parameter description

| Configuration item | Type | Description |
|--------|------|------------------|
| engine_type | string | Engine type, such as `vllm`. |
| **engine_config field** | - |For the parameter description of the `engine_config` field, see [vLLM official parameter configuration](https://docs.vllm.ai/en/latest/api/vllm/config).|
| **motor_nodemanger_config field** |-|-|
| api_config.pod_ip |string | Pod IP (injected by the environment or deployment). Default value: `127.0.0.1` (or `Env.pod_ip`). |
| api_config.node_manager_port |int | NodeManager port. Default value: `1026`. |
| endpoint_config.endpoint_num |int | Number of engine endpoints, usually derived from the HCCL/parallel configuration. Default value: `0`. |
| endpoint_config.base_port |int | Starting port number of the endpoints. Default value: `10000`. |
| endpoint_config.mgmt_ports |array | List of management ports for each endpoint (integer array). Default value: `[]`. |
| endpoint_config.service_ports |array | List of inference service ports for each endpoint (integer array). Default value: `[]`. |
| snapshot_config.enable_snapshot |bool|Master switch for whether to enable the container snapshot feature. Default value: `false`.<br>After it is enabled, users can create snapshot images for instance containers, and instances restored from snapshots can register with the control plane.|
| snapshot_config.snapshot_metadata_path |string|Path of the container snapshot metadata file, which contains the metadata required during container snapshot creation and restoration. Empty by default.|
| logging_config.log_level | string | Log level. Default value: `INFO`<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul>|
| logging_config.log_max_line_length | int | Maximum length of a single log entry; lines exceeding this limit are truncated. Default value: `8192`. |
| logging_config.log_format | string | Log format template, supporting Python logging placeholders. Default value: `"(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s"`. |
| logging_config.log_date_format | string | Log date format. Default value: `"%m-%d %H:%M:%S"`. |
| logging_config.host_log_dir | string | Log storage path. Default value: `"/root/ascend/log"`.|
| logging_config.log_rotation_size | int | Log dump file size. Default value: `20`.|
| logging_config.log_rotation_count | int |Log dump file count. Default value: `10`.|
| logging_config.log_compress |bool| Whether to enable log compression. Default value: `false`.|
| logging_config.log_compress_level |int|Log compression level. Default value: `6`.|
| logging_config.log_max_total_size |int|Total log file size, in MB. Default value: `200`.|
| logging_config.log_cleanup_interval |int|Log cleanup interval, in seconds. Default value: `1800`.|
| logging_config.log_collector_enabled |bool|Whether to enable Collector logs. Default value: `true`.|
| logging_config.third_party_log_levels |string|Third-party log level. Default value: `WARNING`.<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul>|
| port_allocator_config.enable |bool|Whether to enable automatic port allocation. Default value: `true`.|
| port_allocator_config.scan_range |int|Port scanning range. Default value: `100`.|
| port_allocator_config.probe_timeout_seconds |float|Probe timeout duration. Default value: `0.5`.|
| port_allocator_config.remote_check_timeout_seconds |float|Remote detection timeout duration. Default value: `1.0`.|
| port_allocator_config.bind_host |string|Host address to be bound. Default value: `0.0.0.0`.|

## motor_engine_prefill_config`/`motor_engine_decode_config

The `motor_engine_prefill_config` and `motor_engine_decode_config` fields are used in the **PD disaggregation deployment scenario**, and these two fields configure the Prefill and Decode engines respectively. Both have the same structure and require specifying `engine_type` and `engine_config`; `dispatch_profile` (PD collaborative semantics) and `health_check_config` (virtual push health probe, see [virtual push health probe](../features/sim_inference.md)) are optional. A configuration example is shown below.

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

**Table 5** `motor_engine_prefill_config` and `motor_engine_decode_config` field parameter description

| Configuration item | Type | Description |
|--------|------|------------------|
| engine_type | string | Engine type, such as `vllm`. |
| **engine_config field** | - | For the parameter description of the engine_config field, see details in [vLLM official parameter configuration](https://docs.vllm.ai/en/latest/api/vllm/config). |
| **motor_nodemanger_config field** |-|-|
| api_config.pod_ip |string | Pod IP (injected by the environment or deployment). Default value: `127.0.0.1` (or `Env.pod_ip`). |
| api_config.node_manager_port |int | NodeManager port. Default value: `1026`. |
| endpoint_config.endpoint_num |int | Number of engine endpoints, usually derived from the HCCL/parallel configuration. Default value: `0`. |
| endpoint_config.base_port |int | Starting port number of the endpoints. Default value: `10000`. |
| endpoint_config.mgmt_ports |array | List of management ports for each endpoint (integer array). Default value: `[]`. |
| endpoint_config.service_ports |array | List of inference service ports for each endpoint (integer array). Default value: `[]`. |
| snapshot_config.enable_snapshot |bool|Master switch for whether to enable the container snapshot feature. Default value: `false`.<br>After it is enabled, users can create snapshot images for instance containers, and instances restored from snapshots can register with the control plane.|
| snapshot_config.snapshot_metadata_path |string|Path of the container snapshot metadata file, which contains the metadata required during container snapshot creation and restoration. Empty by default.|
| logging_config.log_level | string | Log level. Default value: `INFO`<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul>|
| logging_config.log_max_line_length | int | Maximum length of a single log entry, truncate if exceeded. Default value: `8192`. |
| logging_config.log_format | string | Log format template, supporting Python logging placeholders. Default value: `"(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s"`. |
| logging_config.log_date_format | string | Log date format. Default value: `"%m-%d %H:%M:%S"`. |
| logging_config.host_log_dir | string | Log storage path. Default value: `"/root/ascend/log"`.|
| logging_config.log_rotation_size | int | Log dump file size. Default value: `20`.|
| logging_config.log_rotation_count | int |Log dump file count. Default value: `10`.|
| logging_config.log_compress |bool| Whether to enable log compression. Default value: `false`.|
| logging_config.log_compress_level |int|Log compression level. Default value: `6`.|
| logging_config.log_max_total_size |int|Total size of log files, unit: MB. Default value: `200`.|
| logging_config.log_cleanup_interval |int|Log cleanup interval, unit: second. Default value: `1800`.|
| logging_config.log_collector_enabled |bool|Whether to enable Collector logs. Default value: `true`.|
| logging_config.third_party_log_levels |string|Third-party log level. Default value: `WARNING`.<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul>|
| port_allocator_config.enable |bool|Whether to enable automatic port allocation. Default value: `true`.|
| port_allocator_config.scan_range |int|Port scanning range. Default value: `100`.|
| port_allocator_config.probe_timeout_seconds |float|Probe timeout duration. Default value: `0.5`.|
| port_allocator_config.remote_check_timeout_seconds |float|Remote detection timeout duration. Default value: `1.0`.|
| port_allocator_config.bind_host |string|Host address to be bound. Default value: `0.0.0.0`.|

In PD mode, **configure `health_check_config` independently** for P and D, and the code default values are used when not configured.

### dispatch_profile

When `engine_config.kv_transfer_config.kv_connector` is not in the built-in whitelist identification, you can explicitly declare the P/D collaborative semantics at the **top level** of `motor_engine_prefill_config`/`motor_engine_decode_config`. NodeManager derives `dispatch_capabilities` from this and reports it to the Coordinator.

**Table 6** `dispatch_profile` parameter description

| configuration item | Type | Description |
|--------|------|--------|
| dispatch_profile | string | P/D collaborative semantics. Default value: inferred from the kv_connector whitelist when not configured.<br>Optional values: <ul><li>`handoff`: Handed over to Decode after Prefill completes. The derived capability is `prefill_handoff_decode`.</li><li>`trigger`: P/D concurrent, with the engine synchronizing KV. The derived capability is `concurrent_engine_sync`.</li></ul>Prefill and Decode **must be configured with the same value on both ends**. |

Connectors in the whitelist do not require manual configuration of dispatch_profile.

>[!NOTE]NOTE
>
>- `dispatch_profile` is written at the top level of `motor_engine_*_config`, not inside the `engine_config` field.
>- Users are not supported to directly fill in `dispatch_capabilities`; it will be discarded by NodeManager after configuration.
>- The value must be consistent with the actual collaborative semantics of the connector. When P/D are inconsistent or have no common capability, the Coordinator route returns `503`.

**Configuration example** (custom connector):

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

Optional virtual inference health probe configuration, located in the `motor_engine_prefill_config`/`motor_engine_decode_config` module. It is disabled by default. For the mechanism description, see [virtual push health](../features/sim_inference.md).

**Table 7** `health_check_config` field parameter description

| Configuration item | Type | Description |
|--------|------|--------|
| enable_virtual_inference | bool | Master switch for virtual inference. Default value: `false`.<br>When set to `true`, periodic virtual inference starts after the inference plane `/health` returns normal.<br>**It can be enabled only at the ERROR log level** (`ASCEND_GLOBAL_LOG_LEVEL=3`; ERROR is used by default when not configured). If a non-ERROR level is explicitly configured, the Engine Server disables it and prints a warning before starting virtual inference. |
| npu_usage_threshold | int | AI Cube utilization threshold (%), default value: `3`.<br>Virtual inference starts only when `0 < npu_usage_threshold <= 100`. When the utilization is below this threshold and virtual inference fails, the cumulative failure count increases by 1. |
| max_failure_count | int | Maximum number of consecutive virtual inference failures (after the accumulation condition is met), default value: `6`.<br>When this limit is reached, the Engine Server `/status` returns abnormal. |
| health_collector_timeout | int | Probe timeout for the inference plane `GET /health` (in seconds), default value: `5`. |
| health_collector_timeout_retry_attempts | int | Number of timeout retries for the inference plane `GET /health` (including the first attempt), default value: `3`.<br>Retries occur only on probe timeout; other exceptions such as connection failure and HTTP errors are not retried. |

## Other Parameter Descriptions

### motor_engine_union_env

In the PD co-location scenario, the environment variables of the union Engine Server are configured in `motor_engine_union_env` of `env.json`. For an example, see `examples/infer_engines/vllm/pd_hybrid/env.json`.

**Configuration example**:

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

**Table 8** `motor_engine_union_env` field parameter description

| Configuration Item | Description |
|--------|------|
| motor_common_env | Environment variables shared by all components, such as the CANN installation path and the log root directory. |
| motor_engine_union_env | NPU, HCCL, OMP, and other environment variables of the PD hybrid union instance, which can be tuned by machine model and model. |

### Automatic Derivation of`prefill_kv_event_config

This field is merged by the Coordinator when `user_config.json` is loaded, and generally no manual intervention is required.
The Coordinator automatically identifies the P/D disaggregation or union hybrid topology based on the instance role, and derives the `dispatch_capabilities` reported internally by NodeManager based on the engine Connector to select concurrent or handoff behavior. This field does not support explicit user configuration; a custom Connector can declare semantics using `dispatch_profile` at the top level of `motor_engine_prefill_config`/`motor_engine_decode_config`. For details, see [dispatch_profile](#dispatch_profile).
For the Connector whitelist identification, the rule that `MultiConnector` takes `connectors[0]`, and the handling of unrecognized connectors causing route 503 (fail-closed), see [PD Disaggregation Service Deployment](../deployment/k8s/pd_disaggregation_deployment.md).

**Table 9** Description of `prefill_kv_event_config`

| Source | Description |
|------|------|
| PD disaggregation | Derived from `motor_engine_prefill_config.engine_config.kv-events-config` |
| PD co-location | Derived from `motor_engine_union_config.engine_config.kv-events-config` |
| kv_conductor_config | `http_server_port` is written to `prefill_kv_event_config.http_server_port`; defaults to `13333` when not configured |
