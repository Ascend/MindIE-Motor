# Full Parameter Description of the user_config.json Configuration File

This document describes in detail all configurable items of components such as Controller and Coordinator in the `user_config.json` configuration file, whose structure corresponds to the structure of `examples/features/config_sample.json`. During deployment, the system merges the corresponding modules in `user_config.json` into the component runtime configuration, following the principle of "code defaults take precedence unless overridden by user configuration". In addition, dynamic activation is supported by modifying the configuration file monitored by the component. The configuration file is located in the `model_configs/` directory (for example, `model_configs/vllm/user_config.json`). Select the corresponding configuration based on the engine type and model actually used.

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
| p_instances_num | int | Number of P instances. Value range: [1,16]. |
| d_instances_num | int | Number of D instances. Value range: [1,16]. |
| single_p_instance_pod_num | int | Number of Pods corresponding to a single P instance. Value range: greater than or equal to 1. |
| single_d_instance_pod_num | int | Number of Pods corresponding to a single D instance. Value range: greater than or equal to 1. |
| p_pod_npu_num | int | Number of NPU cards occupied by a single P instance Pod. Each Pod supports a maximum of 16 cards. |
| d_pod_npu_num | int | Number of NPU cards occupied by a single D instance Pod. Each Pod supports a maximum of 16 cards. |
| image_name | string | Default inference image (must include the runtime environment such as MindIE Motor and vLLM). It is used when Controller / Coordinator / Prefill / Decode does not configure `image_name` separately. |
| job_id | string | Deployment task name, also used as the K8s namespace, for example, "mindie-motor". |
| hardware_type | string | Hardware type: <ul><li>Atlas 800I A2: `800I_A2`</li><li>Atlas 800I A3: `800I_A3`</li><li>Ascend 950PR&DT products: `Ascend950`</li></ul> The `nodeSelector` of the Atlas 800I A2 and the Atlas 800I A3 contains `accelerator: huawei-Ascend910` and `accelerator-type` (the accelerator is the same for the Atlas 800I A2 and the Atlas 800I A3, so `accelerator-type` must be used to distinguish the product form); for Ascend 950PR&DT products, it is `accelerator: huawei-npu` without `accelerator-type`.<br>**Recommended as mandatory in active/standby and fault tolerance scenarios**: when not configured, the hardware is treated as unknown by default, and linkdown (CardNetworkUnhealthy) faults are stored according to the legacy behavior and may suppress ENGINE_DEAD faults and block engine restart; after the field is configured as a non-Atlas 800I A2 (such as 800I_A3), link false alarms are ignored as noise and no longer affect fault tolerance decisions. |
| weight_mount_path | string | Model weight mount path on the host machine. The `model` path in the container must be consistent with this mount path, for example, `"/mnt/weight/"`. When the standard deployer is used, this path is mounted to both the P/D (or Union) engine and the Coordinator; after `context_budget_mode: "on"` is enabled, the Coordinator must also be able to read the tokenizer files in `engine_config.model`. This field uses `hostPath`, so this path must exist on all nodes to which the Coordinator may be scheduled, or its scheduling scope must be restricted through `coordinator_node_selector`. |
| tls_config | object | Optional; TLS-related configuration, including five types: `mgmt_tls_config`, `infer_tls_config`, `etcd_tls_config`, `grpc_tls_config`, and `observability_tls_config`. |

## Component-Level Kubernetes Labels and Annotations<a id="additional-labels-annotations"></a>

The following component configurations support adding custom metadata to the generated Kubernetes resources at the top level through `additional_labels` and `additional_annotations`:

| Component Configuration | Effective Component |
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

A configuration example is as follows:

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

| Configuration Item | Type | Description |
|--------|------|------|
| additional_annotations | object | Optional; key-value pairs to be added to the `metadata.annotations` of the component workload and its Pod template. Not added by default. When a custom key has the same name as an existing key in the template, the value configured here takes precedence. Keys and values must comply with the Kubernetes Annotation specification. |
| additional_labels | object | Optional; key-value pairs to be added to the `metadata.labels` of the component workload and its Pod template. Not added by default. Except for deployer-reserved labels, when a custom key has the same name as an existing key in the template, the value configured here takes precedence. Keys and values must comply with the Kubernetes Label specification. |

The preceding fields modify only the metadata of the workload and its Pod template, not other resources such as the corresponding Service. In `single_container` deployment mode, all components share the same workload, and custom metadata is configured through `motor_coordinator_config`.

>[!NOTE]NOTE
>
>`app` is a deployer-reserved label and cannot be configured through `additional_labels`; the main deployment process rejects such configurations before generating resources. Do not overwrite other system Labels generated by the deployer, such as component identifiers and workload selectors. This feature does not synchronously modify `spec.selector`, and overwriting such Labels may cause the workload to fail to be created or fail to match Pods.

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
    "master_lock_ttl": 15,
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

**Table 2** `motor_controller_config` field parameter description

| Configuration Item | Type | Description |
|--------|------|------------------|
| **logging_config field** |-|-|
| log_level | string | Log level, default value: `INFO`.<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul> |
| log_max_line_length | int | Maximum length of a single log entry, truncate if exceeded. Default value: `8192`. |
| log_format | string | Log format template, supports Python logging placeholders. Default value: `"(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s \[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s"`. |
| log_date_format | string | Log date format, default value: `"%m-%d %H:%M:%S"`. |
| host_log_dir | string | Log storage path, default value: `"/root/ascend/log"`. |
| log_rotation_size | int | Log dump file size, default value: `20`. |
| log_rotation_count | int | Log dump file count, default value: `10`. |
| log_compress |bool| Whether to enable log compression, default value: `false`. |
| log_compress_level |int| Log compression level, default value: `6`. |
| log_max_total_size |int| Total log file size, unit: MB, default value: `200`. |
| log_cleanup_interval |int| Log cleanup interval, unit: seconds, default value: `1800`. |
| third_party_log_levels | string | Third-party log level, default value: `WARNING`.<ul><li>`DEBUG`</li><li>`INFO`</li><li>`WARNING`</li><li>`ERROR`</li></ul>|
| **api_config field** |-|-|
| controller_api_host | string | Controller API listening address (IP or hostname), default value: `127.0.0.1` (or Env.pod_ip). |
| controller_api_dns | string | Controller API domain name, default value: "mindie-motor-controller-service.mindie-motor.svc.cluster.local".|
| controller_api_port | int | Controller API port, default value: `1026`. |
| observability_api_port | int | Controller observability API port, default value: `1027`.|
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
| enable_fault_tolerance | bool | Whether to enable Motor Controller fault self-healing (advanced RAS), default value: true. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul>This field is neither equivalent to nor automatically passed through as `--enable-fault-tolerance` of the vLLM development branch. |
| strategy_center_check_interval | int | Strategy center polling interval, unit: seconds, default value: `10`. |
| configmap_namespace |string|configmap namespace, default value: "kube-system".|
| configmap_prefix |string|configmap prefix, default value: "mindx-dl-deviceinfo-".|
| k8s_cert_path |string|Security certificate path, empty by default.|
| enable_scale_p2d | bool | Whether to enable ScaleP2D elastic scaling, default value: true. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul> |
| enable_token_reinference | bool | Whether to enable Token Reinference fault recovery, default value: true. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul> |
| scale_p2d_d_instance_reinit_wait_timeout |int|Maximum time to wait for the D instance to self-recover (reinitialize) before ScaleP2D performs preemption, unit: seconds, default value: `60`.<br>During the waiting period, if the D instance recovers to initial/active, ScaleP2D is no longer executed; after timeout, if the D instance is still in a preemptible state such as inactive, the subsequent P instance selection process continues.|
| enable_dp_scale_down | bool | Switch for the Controller to uniformly control DP domain scale-in, default value: false. This switch applies to both Prefill and Decode instances and does not need to be repeatedly configured in `motor_engine_prefill_config` and `motor_engine_decode_config`. The existing Controller master switch `enable_fault_tolerance` defaults to true; if it is explicitly disabled, the overall FT process (including DP scale-in) is not executed, but the Controller startup is not blocked. This switch does not automatically enable engine-side FT, external-lb, MC2 Mask, EP, or EPLB. When engine capabilities are insufficient, FtGate rejects the scale-in and enters the original fault recovery process.<br>When the DP master switches, `engine_config.data-parallel-master-port` is used as the store base port; NodeManager generates the `dp_store_port` of the current round as "base port + global DP rank of the new master". For example, when the base port is 29500, DP1 and DP2 become the master and use 29501 and 29502 respectively, to avoid reusing a listening port that is still occupied when the master switches continuously within the same Pod. This port is used only when the current master is removed and master reselection occurs; the TCPStore is not rebuilt in scale-in rounds where the master does not change. When DP scale-in is enabled, reserve the port range `[data-parallel-master-port, data-parallel-master-port + dp_size - 1]`; the configuration loading phase verifies that this range does not exceed 65535.|
| enable_dp_scale_up | bool | DP scale-out capability switch (currently a tentative name), default value: false. After a successful scale-in, Motor stops the corresponding NodeManager only when this switch is enabled and all DP ranks in the Pod have been removed, and the cluster reclaims and replenishes capacity; when it is disabled, the empty Pod is retained and the card-level scale-in result is not affected.|
| fault_collection_timeout_seconds | float | Timeout for the Controller to collect engine fault states in the same DP domain, unit: seconds, value range `(0, 600]`, default value: 60.0. After the first `UNHEALTHY` or `DEAD` is received, the Coordinator is immediately notified to isolate the instance and the timer starts; it is no longer derived from the engine `cpu-distributed-timeout-seconds`.|
| dp_scale_down_config.request_timeout_sec | float | Timeout for the Controller to call the NodeManager FT interface, unit: seconds, value range `(0, 60]`, default value: 11.0. This value must leave a response margin for the default 5-second request from NodeManager to the engine.|
| dp_scale_down_config.poll_interval_sec | float | Interval for querying the engine FT state during scale-in execution, unit: seconds, value range `(0, 60]`, default value: 1.0.|
| dp_scale_down_config.execution_deadline_sec | float | Motor-side deadline for waiting for the engine to complete scale-in recovery, unit: seconds, value range `[1, 600]`, default value: 30.0. If the engine is still recovering when the deadline is reached, the original fault recovery process is entered directly.|
| **observability_config field**|-|-|
| observability_enable |bool|Whether to enable observability, default value: false. Values are as follows:<ul><li>`true`: enabled</li><li>`false`: disabled</li></ul>|
| metrics_ttl |int|metrics query interval, unit: seconds, default value: `5`.|
| **standby_config field** |-|-|
| enable_master_standby | bool | Whether to enable Controller master/standby. Options: `true` / `false`, default value: `false`. |
| master_standby_check_interval | int | Primary/standby role probe interval (seconds), default value: `5`. |
| master_lock_ttl | int | Lease duration for the primary node to hold the lock on ETCD (seconds), default value: `15`. |
| master_lock_retry_interval | int | Retry interval for acquiring the lock when contending for primary (seconds), default value: `5`. |
| master_lock_max_failures | int | If consecutive master contention failures exceed this count, give up and switch over, default value: `3`. |
| master_lock_key | string | Lock path of the primary node in ETCD; the prefix `/controller/` is automatically added at runtime, default value: `/master_lock` (actually `/controller/master_lock`). |
| **etcd_config field** |-|-|
| etcd_host | string | ETCD service address (hostname or IP), default value: `etcd.default.svc.cluster.local`. |
| etcd_port | int | ETCD port, default value: `2379`. |
| etcd_timeout | int | ETCD operation timeout duration (seconds), default value: `5`.|
| etcd_lb_policy | string | ETCD load balancing policy, default value: round_robin.|
| enable_etcd_persistence | bool | Whether to enable ETCD persistence. Options: `true` / `false`, default value: `false`. |
| **port_allocator_config field** |-|-|
| enable |bool|Whether to enable automatic port allocation, default value: true.|
| scan_range |int|Port scanning range, default value: 100.|
| probe_timeout_seconds |float|Probe timeout duration, default value: 0.5.|
| remote_check_timeout_seconds |float|Remote detection timeout duration, default value: 1.0.|
| bind_host |string|Host address to be bound, default value: 0.0.0.0.|
|**precision_auto_recovery_enable**|bool|Master switch for precision detection. When disabled, no additional sampling injection or detection is performed, and user-requested logprobs are not affected. Options: `true` / `false`, default value: `false`.|
| **additional_annotations** | object | Optional; custom Annotations of the Controller workload and its Pod template. |
| **additional_labels** | object | Optional; custom Labels of the Controller workload and its Pod template. |
| image_name | string | Optional; Controller container image. If not specified, `motor_deploy_config.image_name` is used. **Takes effect only in Kubernetes deployment**; Docker deployment does not support specifying images per component and uniformly uses `motor_deploy_config.image_name`. |

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
    "decode_first_token_timeout": 480,
    "infer_timeout": 3600,
    "upstream_error_body_max_bytes": 65536
  },
  "circuit_config": {
    "enable": true,
    "failure_threshold": 3,
    "base_timeout_s": 30.0,
    "max_timeout_s": 300.0
  },
  "context_budget_mode": "off",
  "render_config": {
    "enable": false,
    "enable_streaming": false,
    "endpoint": {
      "host": "127.0.0.1",
      "port": 8100
    },
    "timeout_ms": 5000,
    "image_name": "",
    "launch_args": {
      "renderer_num_workers": 4
    }
  },
  "token_obfuscation_config": {
    "enable": false,
    "vocab_size": 151936,
    "token_white_list": [151643, 151644, 151645, 151646, 151647, 151648, 151649, 151650, 151651, 151652, 151653, 151654, 151655, 151656, 151657, 151658, 151659, 151660, 151661, 151662, 151663, 151664, 151665, 151666, 151667, 151668, 198, 2610, 525, 264, 10950, 17847, 13, 872, 77091, 8948, 271],
    "seed_content": "",
    "image_config": {
      "enable": false,
      "model_path": "",
      "patch_size": 0,
      "merge_size": 0,
      "longest_edge": 0,
      "shortest_edge": 0,
      "temporal_patch_size": 0
    }
  },
  "scheduler_config": {
    "prefill_scheduler_type": "load_balance",
    "decode_scheduler_type": "load_balance",
    "enable_pd_separation_fallback_to_hybrid": true,
    "endpoint_instance_score_weight": 0.05,
    "dynamic_bucket_short_median": 16384,
    "dynamic_bucket_long_median": 98304,
    "dynamic_bucket_border": 32768,
    "dynamic_bucket_length_scale": 1.0,
    "dynamic_bucket_load_scale": 4.0,
    "dynamic_bucket_short_bucket_count": 0,
    "dynamic_bucket_long_bucket_count": 0,
    "dp_stats_window": 60,
    "kv_affinity": {
      "mode": "unified",
      "load_weight": 1.0,
      "overlap_credit": 1.0,
      "prefill_load_scale": 1.0,
      "load_gate_topn": 0,
      "w_npu": 1.0,
      "w_cpu": 1.0,
      "w_disk": 0.0,
      "hit_rate_threshold": 0.0
    },
    "kv_conductor_config": {
      "conductor_service": "",
      "http_server_port": 13333,
      "query_encoding": "msgpack",
      "block_size": 128,
      "engine_type": "vLLM",
      "model_path": "",
      "store_backend": "",
      "pool_endpoint": "",
      "npu_endpoint": "",
      "cpu_endpoint": "",
      "disk_endpoint": "",
      "endpoint": "",
      "replay_endpoint": "",
      "re_register_interval_sec": 0
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
    "keep_alive_timeout": 60,
    "engine_client_keepalive_expiry": 3.0
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
    "master_lock_ttl": 15,
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

**Table 3** `motor_coordinator_config` field parameter description

| Configuration Item | Type | Description |
|--------|------|------------------|
| context_budget_mode | string | Before request routing, use the model tokenizer to calculate the prompt token count, and trim the effective `max_tokens` / `max_completion_tokens` to the model's remaining context. Applicable to `load_balance`, `round_robin`, and `kv_cache_affinity`. Optional values: `off` (default) or `on`; when `on` is enabled, the P/D engine configuration must provide `model` and `max_model_len`. |
| **render_config field** |-|-|
| enable | bool | Whether to enable the vLLM Render Sidecar. Default value: `false`. After it is enabled, the Coordinator preferentially calls Render; when Render is unavailable, times out, or the interface is not supported, it falls back to the local tokenizer, and request validation errors (400/422, and 404 with a structured error response) are returned directly to the client. vLLM 0.30+ adds an `--enable-scale-out` gate for token-only Generate, and Motor automatically enables it for P/D/Union engines when supported, with no manual configuration required. It is recommended to enable it together with KV Cache affinity scheduling. |
| enable_streaming | bool | Whether the Decode of streaming requests uses token-only Generate and Derender. Experimental switch, default value: `false`; in this case, Render still participates in pre-routing tokenization and PD Prefill, and Decode uses the native vLLM OpenAI streaming response. When set to `true`, Decode switches to token-only and performs Derender chunk by chunk, which requires vLLM >= 0.27.0. Advanced features can be enabled as needed. |
| endpoint | object | Render Sidecar address. The default value of `host` is `127.0.0.1`, and Sidecar deployment supports only local addresses; the default value of `port` is `8100`. |
| timeout_ms | int | HTTP timeout for Render, Derender, and health checks, unit: milliseconds. Default value: `5000`. |
| image_name | string | Render Sidecar image. When configured, this independent image is used; when empty, `motor_deploy_config.image_name` is reused. Empty by default. |
| launch_args | object | Optional `vllm launch render` parameters; no configuration is required when there are no additional startup parameters. If `renderer_num_workers` is not configured, `4` is passed by default. The object is serialized as a single JSON parameter value, and arrays are expanded into multiple parameter values. When configuring parsing parameters such as `tool_call_parser` and `reasoning_parser`, non-streaming Derender requires vLLM >= 0.25.0, and streaming Derender requires vLLM PR #50550. The model, listening address, port, served model name, and access log filtering are managed by Motor and cannot be overridden here. For available parameters, see [vLLM Render CLI](https://docs.vllm.ai/en/latest/api/vllm/entrypoints/cli/launch/). |
| **token_obfuscation_config field** |-|-|
| enable | bool | Whether to enable token obfuscation that works with data obfuscation weights. Default value: `false`. After it is enabled, streaming and non-streaming vLLM Render token-only requests are supported; any obfuscation or Render failure rejects the request without falling back to native OpenAI inference. |
| model_path | string | Optional; explicitly specifies the served weight directory (from which `vocab_size` is read). Empty by default, in which case `motor_engine_{prefill,decode,encode,union}_config.engine_config.model` is used (it must be explicitly specified when multiple models coexist). |
| vocab_size | int | Obfuscated vocabulary size. **Default value: `0` (not configured)**; when not configured, it is read from `config.json` in the weight directory (for multimodal models, `text_config.vocab_size` is used; for text-only models, the top-level `vocab_size` is used), and an explicit configuration always takes precedence. It must be consistent with the weight obfuscation parameters (`151936` for Qwen3-32B / Qwen3-VL). |
| token_white_list | list[int] | Token IDs that do not participate in permutation (special tokens, etc.). Empty by default; it must be explicitly configured when token obfuscation is enabled and must be consistent with the weight obfuscation parameters; for the 37 whitelist tokens used by the verified Qwen3-32B / Qwen3-VL weights, see [data obfuscation inference (PMCC)](../features/data_obfuscation.md#usage-example). This parameter **cannot be derived from the model directory** and can only be provided by the weight obfuscation party. |
| seed_content | string | Data obfuscation factor. Empty by default; it must be explicitly configured when token obfuscation is enabled. This value can restore the vocabulary permutation, must be kept with the same permissions as the obfuscation weights, must be injected through controlled configuration, and must not be committed to a code repository or written to logs. |
| **token_obfuscation_config.image_config field** |-|-|
| enable | bool | Whether to perform data obfuscation on the multimodal image tensors returned by Render (used with the vision part of the data obfuscation weights). Default value: `false`. After it is enabled, the Coordinator calls the SDK's `image_render_obf` for the tensor payload of each modality in `features.kwargs_data`, and then forwards the obfuscated payload to the engine; when Render is not enabled or the SDK throws an error, the request fails (fail closed), and plaintext tensors are never sent to the engine. The correctness of the obfuscation result (permutation content and payload format) is guaranteed by the SDK, and the Coordinator does not parse or structurally validate it. **Requires `render_config.enable=true`**, is independent of `token_obfuscation_config.enable`, and shares the same `seed_content`. |
| model_path | string | Optional; explicitly specifies the served weight directory (from which the geometry parameters are read). Empty by default, in which case `motor_engine_{prefill,decode,encode,union}_config.engine_config.model` is used (it must be explicitly specified when multiple models coexist). |
| patch_size | int | Vision patch edge length. **Default value: `0` (not configured)**; when not configured, it is read from `preprocessor_config.json` in the weight directory, and an explicit configuration always takes precedence. It must be consistent with the `patch_size` used for weight obfuscation (`16` for Qwen3-VL). |
| merge_size | int | Patch merge factor. **Default value: `0` (not configured)**; when not configured, it is read from the weight directory, and an explicit configuration takes precedence. It is consistent with the `merge_size` of the image processor (`2` for Qwen3-VL). |
| longest_edge | int | Maximum number of image pixels. **Default value: `0` (not configured)**; when not configured, it is read from the weight directory (`size.longest_edge` and the legacy `max_pixels` are supported), and an explicit configuration takes precedence. It is `16777216` for Qwen3-VL. |
| shortest_edge | int | Minimum number of image pixels. **Default value: `0` (not configured)**; when not configured, it is read from the weight directory (`size.shortest_edge` and the legacy `min_pixels` are supported), and an explicit configuration takes precedence. It is `65536` for Qwen3-VL. |
| temporal_patch_size | int | Patch length in the temporal dimension. **Default value: `0` (not configured)**; when not configured, it is read from the weight directory, and an explicit configuration takes precedence. It is consistent with the `temporal_patch_size` of the video/image processor (`2` for Qwen3-VL). |
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
| max_retry | int | Maximum number of retries after a request fails. Default value: `5`. |
| transport_max_retry | int/null | Maximum number of attempts for Coordinator transport failures; when `null`, `max_retry` is used. Default value: `null`. |
| retry_delay | float | Wait time before each retry (seconds). Default value: `0.2`. |
| first_token_timeout | int | Timeout duration for waiting for the first token to be returned (seconds). Default value: `600`. |
| decode_first_token_timeout | int | Timeout duration for a streaming Decode request to wait for the first token to be returned (seconds). Default value: `480`. It applies only to streaming Decode forwarding; other scenarios still use the original timeout configurations. When `deploy.py` is used for deployment, this field automatically uses the finally effective `VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT` of Prefill: `motor_engine_prefill_env` takes precedence, otherwise it falls back to `motor_common_env`. |
| infer_timeout | int | Total timeout duration for a single inference request (seconds). In non-streaming scenarios, it applies to a single forwarding; in streaming scenarios, it serves as the overall wall-clock timeout of the entire streaming request (counted from the arrival of the request; after the timeout, the request is interrupted and 504 is returned). Default: `3600`. |
| upstream_error_body_max_bytes | int | Maximum number of bytes of the engine HTTP error body to pass through to the client, to avoid returning oversized error responses. Default value: `65536`. |
| **reschedule_config field** |-|-|
| enable | bool | Switch for the request rescheduling feature. Default value: `false`.<br>Model recomputation is handled by the engine side, and this configuration does not control engine-side recomputation; `recompute_enabled` is only a legacy compatibility alias for `reschedule_enabled`; `recompute_max_retry` has been removed and will be ignored. |
| **circuit_config field** |-|-|
| enable | bool | Master switch for Coordinator self circuit breaking. After it is disabled, request failures are no longer counted and instances are never circuit-broken. Default value: `true`. Configuration changes take effect after the Coordinator is restarted. |
| failure_threshold | int | Circuit breaking is triggered after the number of consecutive failures reaches this value (any success in between resets the count). Default value: `3`. |
| base_timeout_s | float | Duration of the first circuit breaking (seconds), which is also the base of the exponential backoff of the circuit breaking duration; each re-breaking/probe failure grows by `2^(number of circuit breaking - 1)` times. Default value: `30.0`. |
| max_timeout_s | float | Upper limit of the circuit breaking duration (seconds). Default value: `300.0`. |
| **scheduler_config field** |-|-|
| prefill_scheduler_type | string | Scheduling type of Prefill / encode / union instances, default value: load_balance. `load_balance` / `round_robin` / `kv_cache_affinity` are supported; `dynamic_bucket` is not supported. |
| decode_scheduler_type | string | Scheduling type of Decode instances, default value: load_balance. `load_balance` / `round_robin` / `dynamic_bucket` are supported. For details about dynamic bucket scheduling, see [dynamic bucket scheduling](../features/dynamic_bucket.md). The enumeration also accepts `kv_cache_affinity`, but Decode instances are still selected by load_balance and a warning is printed at startup. |
| scheduler_type | string | **Deprecated**. Existing configurations can still be used; when read, the value is migrated to `prefill_scheduler_type` and `decode_scheduler_type` that are not configured separately, and a warning is printed. Because Prefill does not support `dynamic_bucket`, dynamic bucket scheduling must be configured separately through `decode_scheduler_type`. |
| enable_pd_separation_fallback_to_hybrid | bool | In PD disaggregation scenarios, when no compatible and non-circuit-broken P/D pair exists, whether to allow fallback to hybrid routing. Default value: `true`. The candidate priority is Union → Prefill → Decode; the Decode fallback applies only to vLLM instances that report the `decode_colocation` capability, and 503 is returned when no compatible pair exists after it is disabled. |
| endpoint_instance_score_weight | float | Weight of the average instance load when endpoint prioritizes load balancing. Default value: `0.05`. |
| dynamic_bucket_short_median | int | Feature length (tokens) of the short-sequence bucket, which is also the Sigmoid midpoint of the short bucket length factor. It must be greater than `0`. Default value: `16384`. |
| dynamic_bucket_long_median | int | Feature length (tokens) of the long-sequence bucket; its difference from `dynamic_bucket_border` is the normalization span of the long bucket length factor. It must be greater than `dynamic_bucket_border`. Default value: `98304`. |
| dynamic_bucket_border | int | Initial boundary between long and short requests (tokens). When the request length is not greater than this value, the short bucket is initially selected; otherwise, the long bucket is initially selected. It must be greater than `0`. Default value: `32768`. |
| dynamic_bucket_length_scale | float | Scaling coefficient of the length factor, which controls the switching slope of the request length near the feature length; it must be a finite value greater than `0`. Default value: `1.0`. |
| dynamic_bucket_load_scale | float | Scaling coefficient of the load factor, which also participates in calculating the load ratio threshold of the short bucket; it must be a finite value greater than `0`. Default value: `4.0`. |
| dynamic_bucket_short_bucket_count | int | Number of short bucket endpoints or the numerator of the bucket ratio; it must not be less than `0`. When it and the long bucket count are both `0`, the endpoints are automatically divided approximately evenly. Default value: `0`. |
| dynamic_bucket_long_bucket_count | int | Number of long bucket endpoints or the numerator of the bucket ratio; it must not be less than `0`. When it is configured together with the short bucket count, the endpoints are allocated according to the ratio of the two. Default value: `0`. |
| dp_stats_window | int | Window (seconds) in which worker 0 periodically prints the number of successfully submitted requests per DP and the SHM `active_tokens`, output on the same line (`dp_stats` log). It is independent of KV affinity hit statistics and takes effect in all deployment and scheduling types; default `60`; `0` disables it. |
| kv_affinity | object | KV Cache affinity scheduling parameters (see the table below) |
| **kv_affinity field** |-|-|
| mode | string | Sub-policy when any role uses `kv_cache_affinity`: `unified` (default) or `load_gated`. |
| load_weight | float | Weight of the endpoint real-time load in unified mode. Default value: `1.0`. |
| overlap_credit | float | Discount factor of the cache prefix on prefill cost. Default value: `1.0`. |
| prefill_load_scale | float | Weight of the prefill cost (after affinity discount) in unified mode. Default value: `1.0`. |
| load_gate_topn | int | In load_gated mode, first retain the N endpoints with the lowest load and then perform affinity-based selection; when `0`, fall back to `2`. Default value: `0`. |
| w_npu | float | Weight of mutually exclusive NPU hit blocks. Default value: `1.0`. |
| w_cpu | float | Weight of mutually exclusive CPU hit blocks. Default value: `1.0`. |
| w_disk | float | Weight of mutually exclusive Disk hit blocks. Default value: `0.0`. |
| hit_rate_threshold | float | Affinity hit rate threshold, value range `[0, 1]`. The default `0` only disables this threshold, and other fallbacks still take effect. When it is greater than 0, the maximum weighted prefix hit rate of all candidates must be **strictly greater than** this threshold; equality also falls back to `load_balance`, and `1` always falls back. The threshold is calculated before `load_gated` filtering, so it is not guaranteed that the final endpoint itself exceeds the threshold. |
| **inference_workers_config field** |-|-|
| num_workers | int | Number of business-plane workers in the Coordinator. Default value: `4`. |
| worker_metaserver_base_port | int | Starting port of the metaserver of each Inference Worker during vLLM layerwise/trigger PD. Default value: `12000`. Worker `i` listens on `base+i` and exposes only `POST /v1/metaserver`. Set it to `0` to disable. Ensure that `base+num_workers-1 <= 65535`. Handoff and trigger cannot be co-located in the same cluster. The listening address prefers `POD_IP`, otherwise `coordinator_api_host` is used (loopback is not bound). `coordinator_api_host=0.0.0.0`/`::` still allows startup; when Trigger is used, a `POD_IP` or a reachable `coordinator_api_host` is required, otherwise the request returns 503. When the port is occupied or the metaserver fails to start, the inference port continues to serve, and Trigger requests of that Worker return 503. |
| **timeout_config field** |-|-|
| request_timeout | int | Timeout duration for a single HTTP request (seconds). Default value: `30`. |
| connection_timeout | int | Timeout duration for establishing a connection (seconds). Default value: `10`. |
| read_timeout | int | Timeout duration for read operations (seconds). Default value: `15`. |
| write_timeout | int | Timeout duration for write operations (seconds). Default value: `15`. |
| keep_alive_timeout | int | Connection keep-alive duration; the connection is closed if no activity occurs within the timeout (seconds). Default value: `60`. |
| engine_client_keepalive_expiry | float | Idle connection expiration time of the HTTP client from the Coordinator to the inference engine (seconds). Default value: `3.0`, which should be less than the keep-alive timeout of the inference engine server (default `5` seconds for vLLM/uvicorn). |
| **api_key_config field** |-|-|
| enable_api_key | bool | Whether to enable API Key authentication. Optional: `true` / `false`. Default value: `false`. |
| valid_keys | array | List of valid API Key strings. Default value: `[]`. |
| header_name | string | Name of the HTTP header carrying the API Key. Default value: `Authorization`. |
| key_prefix | string | Prefix of the Key in the header, such as`Bearer`. Default value: `Bearer`.|
| skip_paths | array | List of paths that do not require API Key validation (such as `/metrics`, `/liveness`, `/docs`, `/v1/metaserver`, etc.), customizable. The code includes `/v1/metaserver` by default (Decode layerwise callbacks do not carry a Key). |
| encryption_algorithm | string | Encryption algorithm used for Key validation, such as `PBKDF2_SHA256`. Default value: `PBKDF2_SHA256` |
| **mgmt_api_key_config field** |-|Independent API Key configuration of the management plane, which protects only the instance query, instance refresh, and precision alarm state cleanup interfaces. Startup, liveness, and readiness probes are not authenticated. The request header is fixed to `X-Motor-Management-Key`.|
| enable_api_key | bool | Whether to enable management-plane API Key authentication. Optional: `true` / `false`. Default value: `false`.|
| api_key_file | string | API Key file path. Mandatory when authentication is enabled; the file must contain only one non-empty key line. When the Controller and the Coordinator are deployed separately, key files with the same content must be mounted.|
| **rate_limit_config field** |-|For behavior and examples, see [rate limiting](../features/rate_limiting.md).|
| enable_rate_limit | bool | Whether to enable request rate limiting. Optional: `true` / `false`. Default value: `false` |
| provider |string|Rate limiting provider. simple uses the built-in token bucket; OLC uses the overload control library (requires additional installation and configuration).|
| max_requests | int | Maximum number of requests allowed within the rate limiting time window. Default value: `1000`. |
| window_size | int | Length of the time window for rate limiting statistics (seconds). Default value: `60`. |
| scope | string | Scope of rate limiting, such as `global`. Default value: `global`. |
| skip_paths | array | List of paths that do not participate in rate limiting statistics (such as `/liveness`, `/readiness`, `/metrics`), customizable. |
| error_message | string | Message returned to the client when rate limiting is triggered. Default value: `too many requests, please try again later`. |
| error_status_code | int | HTTP status code returned when rate limiting is triggered, usually 4xx (such as 429). Default value: `429`. |
| max_request_body_size | float | Maximum request body size (MB); requests exceeding this are rejected directly with 413 and do not consume rate limiting tokens. `= 0` means no limit. Decimal values are supported (such as `0.5` for 0.5MB, 1MB = 1024\*1024 bytes). Default value: `0` (no limit). |
| olc_config_path |string|Absolute path of the OLC rule configuration directory, or a relative path to the service startup directory. The directory must contain overload-config.properties and olc.json.|
| **standby_config field** |-|-|
| enable_master_standby | bool | Whether to enable Coordinator primary/standby. Optional: `true` / `false`. Default value: `false`. |
| master_standby_check_interval | int | Interval for primary/standby role probing (seconds). Default value: `5`; at runtime, the Coordinator automatically limits it to no more than `2`. |
| master_lock_ttl | int | Lease duration for the primary node to hold the lock on ETCD (seconds). Default value: `15`; at runtime, the Coordinator automatically limits it to no more than `8` to ensure that the active/standby switchover can be completed within 30 seconds. |
| master_lock_retry_interval | int | Retry interval for acquiring the lock when competing for primary (seconds). Default value: `5`. |
| master_lock_max_failures | int | If consecutive failures to acquire primary exceed this count, give up and switch over. Default value: `3`. |
| master_lock_key | string | Lock path of the primary node in ETCD; the prefix `/coordinator/` is automatically added at runtime. Default value: `/master_lock` (actually `/coordinator/master_lock`). |
| **etcd_config field** |-|-|
| etcd_host | string |ETCD service address (hostname or IP). Default value: `etcd.default.svc.cluster.local`. |
| etcd_port | int | ETCD port. Default value: 2379. |
| etcd_timeout | int | ETCD operation timeout duration (seconds). Default value: `5`. |
| etcd_lb_policy | string| ETCD load balancing policy, default value: round_robin.|
| enable_etcd_persistence | bool | Whether to enable ETCD persistence. Optional: `true` / `false`. Default value: false. |
| **aigw_model field** |-|This parameter is the centralized configuration of AIGW model metadata, used for model information returned by interfaces such as /v1/models. In user_config.json, it corresponds to the aigw object under motor_coordinator_config; it is null when not used, and its internal configurable items are as follows. At startup, if a complete Prefill/Decode configuration or the `motor_engine_union_config` of PD aggregation exists, **only fields that are not explicitly configured are filled** (P+D takes precedence over union); in the aggregation scenario, the missing `p_max_seqlen` and `d_max_seqlen` are both taken from the `max_model_len` of union.|
| id | string | Model ID, consistent with the model name in OpenAI-compatible interfaces. When not configured, it is auto-filled from the model_name of Prefill (or hybrid union); if explicitly configured, it is retained. |
| object | string | Object type, fixed to `model`. Auto-filled when not configured. |
| owned_by | string | Model ownership identifier, such as `motor`. Auto-filled when not configured. |
| p_max_seqlen | int | Maximum sequence length on the Prefill side (positive integer). When not configured, auto-filled from the `engine_config.max_model_len` of Prefill (or hybrid union). |
| d_max_seqlen | int | Maximum sequence length on the Decode side (positive integer). When not configured, auto-filled from the `engine_config.max_model_len` of Decode (or hybrid union). |
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
| **scheduler_config.kv_conductor_config field** |-|The recommended configuration of a complete `user_config.json` is the top-level `kv_conductor_config`, which is merged by the Coordinator into the runtime path listed in this section when loaded; enabling the service through the deployer still requires `http_server_port` to be explicitly configured at the top level. The legacy `prefill_kv_event_config` retains compatibility for only some fields. For details, see [automatic derivation and compatibility](#kv_conductor_config-automatic-derivation-and-compatibility).|
| conductor_service |string|Conductor service IP or domain name. Empty by default.|
| http_server_port |int|HTTP service port of the KV Conductor. Default value: `13333`, value range: [1024,65535].|
| query_encoding |string|Encoding of the Conductor `/query` request. Default `msgpack`, valid values are `msgpack` / `json`. Responses are decoded according to the server Content-Type, and requests are not automatically downgraded. Upgrade the conductor before upgrading the Coordinator; when the new Coordinator connects to an old conductor that supports only JSON, explicitly set it to `json`. Do not write it under the legacy `prefill_kv_event_config`, because this field is not in the legacy configuration migration list.|
| block_size |int|Positive integer, default `128`. It must be aligned with the granularity of the KV events of the main attention group; DeepSeek V4 / hybrid KV is not necessarily equal to the engine `--block-size`, nor is it fixed at 512. See [hybrid KV configuration](../features/kvcache_affinity.md#deepseek-v4).|
| endpoint |string|Fallback mode of the HBM event port. Empty by default; derived from `kv-events-config.endpoint` of Prefill or Union. The format is `tcp://*:<port>`; when registering HBM, the IP is replaced and the DP rank is appended.|
| replay_endpoint |string|Engine replay port mode. Empty by default; derived from the event configuration of Prefill or Union. The format is `tcp://*:<port>`; when registering HBM, the IP is replaced and the DP rank is appended. Replay is limited by the historical buffer and the first instance registration, and automatic recovery of every DP is not guaranteed.|
| engine_type |string|Engine type. Default value: vLLM.|
| model_path |string|Local tokenizer model directory of the Coordinator. Empty by default and can be derived from the engine model configuration; this value is also used for central pool registration. The registered model name of P/U DP takes the instance `model_name`.|
| store_backend |string|Empty by default; treated as `Mooncake` during registration. `Mooncake` / `Memcache` / `YuanRong` are supported.|
| pool_endpoint |string|ZMQ address of the Mooncake/Memcache central pool. Empty by default; `*` is replaced with `KVS_MASTER_SERVICE`.|
| npu_endpoint |string|HBM event port override mode. Empty by default; the format is `tcp://*:<port>`, and the DP rank is appended to the port. The legacy alias `xpu_endpoint` is used when this item is empty.|
| cpu_endpoint / disk_endpoint |string|YuanRong node-level CPU/Disk event ports. Empty by default. They are registered by `(IP, model_name)` using the base port without appending the DP rank; explicit configuration is recommended to avoid falling back to the HBM event port.|
| re_register_interval_sec |int|Interval for periodically checking missing registrations (seconds). Default `0`; disabled when less than or equal to 0. It covers P/U DP and YuanRong node pool supplementary registration; it is not equivalent to full cache recovery. |
| **token_sampling_config field** |-|-|
| interval_seconds |float|Minimum interval between two sampling parameter injections for the same Decode instance. Default value: 30.0.|
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
| additional_annotations | object | Optional; custom Annotations of the Coordinator workload and its Pod template. |
| additional_labels | object | Optional; custom Labels of the Coordinator workload and its Pod template. |
| image_name | string | Optional; Coordinator container image. If not specified, `motor_deploy_config.image_name` is used. **Takes effect only in Kubernetes deployment**; Docker deployment uniformly uses `motor_deploy_config.image_name`. |

## motor_engine_union_config

The `motor_engine_union_config` field is used in the **PD aggregation scenario** to configure instances of the same type of union native engine. Its structure is similar to `motor_engine_prefill_config`/`motor_engine_decode_config`, but it does not distinguish between the P/D engine configurations, nor does it require configuring the producer/consumer roles of `kv_transfer_config`. Its configuration example is as follows.

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
      "poll_interval_sec": 1.0,
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

**Table 4** <a id="motor_nodemanger_config"></a>motor_engine_union_config field parameter description

| Configuration item | Type | Description |
|--------|------|------------------|
| engine_type | string | Engine type, such as `vllm`. |
| additional_annotations | object | Optional; custom Annotations of the Union Engine workload and its Pod template. |
| additional_labels | object | Optional; custom Labels of the Union Engine workload and its Pod template. |
| image_name | string | Optional; Union engine container image. If not specified, `motor_deploy_config.image_name` is used. **Takes effect only in Kubernetes deployment**; Docker deployment uniformly uses `motor_deploy_config.image_name`. |
| **engine_config field** | - | `engine_config` is directly mapped to the native startup parameters of the selected engine; see the official parameter documentation of the corresponding vLLM/SGLang version. |
| engine_config.served_model_name | string/array | Optional vLLM service name; when explicitly configured as empty, `engine_config.model` is used. When the field is missing, compatibility with the legacy `model_config.model_name` is retained; when the legacy configuration also does not provide it, `engine_config.model` is used. |
| **motor_nodemanger_config field** |-|-|
| api_config.pod_ip |string | Pod IP (injected by the environment or deployment). Default value: `127.0.0.1` (or `Env.pod_ip`). |
| api_config.node_manager_port |int | NodeManager port. Default value: `1026`. |
| endpoint_config.endpoint_num |int | Number of engine endpoints, usually derived from the HCCL/parallel configuration. Default value: `0`. |
| endpoint_config.base_port |int | Starting port number of the endpoints. Default value: `10000`. |
| endpoint_config.service_ports |array | List of inference service ports for each endpoint (integer array). Default value: `[]`. |
| endpoint_config.bootstrap_port |int/null | Native bootstrap port of SGLang PD. It is derived from `engine_config.disaggregation_bootstrap_port` (compatible with `disaggregation-bootstrap-port`) in the configuration of the selected engine; it is empty for vLLM or when not configured. |
| fault_tolerance_config.enable_fault_tolerance |bool|Whether to explicitly enable engine software fault polling, default value: false.<br>It is automatically enabled when the engine user config detects the FT switch, with no explicit configuration required.|
| fault_tolerance_config.poll_interval_sec |float|Interval for polling the engine FT state, unit: seconds, default value: 1.0.|
| fault_tolerance_config.poll_timeout_sec |float|HTTP timeout of a single poll, unit: seconds, default value: 5.0.|
| fault_tolerance_config.max_poll_failures |int|Threshold of consecutive polling failures; after it is reached, the engine is reported as dead. Default value: 3.|
| snapshot_config.enable_snapshot |bool|Master switch for whether to enable the container snapshot feature. Default value: `false`.<br>After it is enabled, users can create snapshot images for instance containers, and instances restored from snapshots can register with the control plane.|
| snapshot_config.snapshot_metadata_path |string|Path of the container snapshot metadata file, which contains the metadata required during container snapshot creation and restoration. Empty by default.|
| vllm_startup_acceleration_config.enable_startup_plan |bool|Whether to enable vLLM StartPlan, default value: false. When an existing StartPlan is hit, vLLM skips memory profiling.|
| vllm_startup_acceleration_config.enable_graph_reuse |bool|Whether to enable full graph reuse of the vLLM-Ascend backend, default value: false.|
| vllm_startup_acceleration_config.cache_root |string|Optional absolute path for the vLLM StartPlan and compilation cache. When it is not specified, `VLLM_CACHE_ROOT` is inherited; if that is also not specified, the default vLLM directory is used. In production environments, explicitly configure a persistent path.|
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
| **kv_cache_store_config field** |-|-|
| kv_cache_store_config | object | KV pooling configuration (`enable`/`backend`/`store_mode`/`global_segment_size`/`local_buffer_size`/`store_http_port`, etc.). Pooling is not enabled when it is not configured. For complete fields and default values, see [KV Pooling README — kv_cache_store_config](../features/kv_cache_store/README.md); for the Mooncake `standalone` deployment mode, see [Mooncake backend documentation](../features/kv_cache_store/backend/mooncake.md#scenario-2-standalone-mode-configuration). |

## motor_engine_prefill_config/motor_engine_decode_config

The `motor_engine_prefill_config` and `motor_engine_decode_config` fields are used in the **PD disaggregation deployment scenario**, and these two fields configure the Prefill and Decode engines respectively. Both have the same structure and require specifying `engine_type` and `engine_config`; `health_check_config` is used to configure the native `/health` timeout and the model loading startup window. A configuration example is shown below.

DP domain scale-in does not add Motor feature switches to the P/D configurations. To enable scale-in capability for instances on one side, simply enable the native vLLM FT in the
`engine_config` of that side, and configure the actually required capabilities such as external-lb, MC2 Mask, and EP/EPLB;
Prefill and Decode generate capability snapshots separately and pass FtGate independently, and it is not required to enable both sides at the same time. The top-level
`fault_tolerance_config.enable_fault_tolerance` of P/D is only the NodeManager engine state polling switch, which is automatically enabled when
`engine_config.enable_fault_tolerance=true` is detected, with no need for customers to configure it repeatedly.

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
      "poll_interval_sec": 1.0,
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
      "poll_interval_sec": 1.0,
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

**Table 5** `motor_engine_prefill_config` and `motor_engine_decode_config` field parameter description

| Configuration item | Type | Description |
|--------|------|------------------|
| engine_type | string | Engine type, such as `vllm`. |
| additional_annotations | object | Optional; custom Annotations of the Prefill/Decode Engine workload and its Pod template. |
| additional_labels | object | Optional; custom Labels of the Prefill/Decode Engine workload and its Pod template. |
| image_name | string | Optional; Prefill/Decode engine container image. If not specified, `motor_deploy_config.image_name` is used. **Takes effect only in Kubernetes deployment**; Docker deployment uniformly uses `motor_deploy_config.image_name`. |
| npu_chip_name | string | Only for `motor_engine_prefill_config`. Optional; when specified, `huawei.com/npu.chip.name` is added to the Prefill `nodeSelector`, for example, `Ascend950PR`. When not specified, this entry is not added. **Takes effect only in Kubernetes deployment**; it does not take effect in Docker deployment (Docker has no scheduler, and containers are allocated manually according to the machine form; see [Docker multi-container PD disaggregation deployment](../deployment/docker/multi_container.md)). See [PD disaggregation service deployment](../deployment/k8s/pd_disaggregation_deployment.md). |
| npu_chip_name | string | Only for `motor_engine_decode_config`. Optional; when specified, `huawei.com/npu.chip.name` is added to the Decode `nodeSelector`, for example, `Ascend950DT`. When not specified, this entry is not added. **Takes effect only in Kubernetes deployment**; it does not take effect in Docker deployment. |
| **engine_config field** | - | `engine_config` is directly mapped to the native startup parameters of the selected engine; see the official parameter documentation of the corresponding vLLM/SGLang version. |
| engine_config.served_model_name | string/array | Optional vLLM service name; when explicitly configured as empty, `engine_config.model` is used. When the field is missing, compatibility with the legacy `model_config.model_name` is retained; when the legacy configuration also does not provide it, `engine_config.model` is used. |
| **motor_nodemanger_config field** |-|-|
| api_config.pod_ip |string | Pod IP (injected by the environment or deployment). Default value: `127.0.0.1` (or `Env.pod_ip`). |
| api_config.node_manager_port |int | NodeManager port. Default value: `1026`. |
| endpoint_config.endpoint_num |int | Number of engine endpoints, usually derived from the HCCL/parallel configuration. Default value: `0`. |
| endpoint_config.base_port |int | Starting port number of the endpoints. Default value: `10000`. |
| endpoint_config.service_ports |array | List of inference service ports for each endpoint (integer array). Default value: `[]`. |
| endpoint_config.bootstrap_port |int/null | Native bootstrap port of SGLang PD. It is derived from `engine_config.disaggregation_bootstrap_port` (compatible with `disaggregation-bootstrap-port`) in the configuration of the selected engine; it is empty for vLLM or when not configured. |
| fault_tolerance_config.enable_fault_tolerance |bool|Whether to explicitly enable engine software fault polling, default value: false.<br>It is automatically enabled when the engine user config detects the FT switch, with no explicit configuration required.|
| fault_tolerance_config.poll_interval_sec |float|Interval for polling the engine FT state, unit: seconds, default value: 1.0.|
| fault_tolerance_config.poll_timeout_sec |float|HTTP timeout of a single poll, unit: seconds, default value: 5.0.|
| fault_tolerance_config.max_poll_failures |int|Threshold of consecutive polling failures; after it is reached, the engine is reported as dead. Default value: 3.|
| snapshot_config.enable_snapshot |bool|Master switch for whether to enable the container snapshot feature. Default value: `false`.<br>After it is enabled, users can create snapshot images for instance containers, and instances restored from snapshots can register with the control plane.|
| snapshot_config.snapshot_metadata_path |string|Path of the container snapshot metadata file, which contains the metadata required during container snapshot creation and restoration. Empty by default.|
| vllm_startup_acceleration_config.enable_startup_plan |bool|Whether to enable vLLM StartPlan, default value: false. When an existing StartPlan is hit, vLLM skips memory profiling.|
| vllm_startup_acceleration_config.enable_graph_reuse |bool|Whether to enable full graph reuse of the vLLM-Ascend backend, default value: false.|
| vllm_startup_acceleration_config.cache_root |string|Optional absolute path for the vLLM StartPlan and compilation cache. When it is not specified, `VLLM_CACHE_ROOT` is inherited; if that is also not specified, the default vLLM directory is used. In production environments, explicitly configure a persistent path.|
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
| **kv_cache_store_config field** |-|-|
| kv_cache_store_config | object | KV pooling configuration (`enable`/`backend`/`store_mode`/`global_segment_size`/`local_buffer_size`/`store_http_port`, etc.). Pooling is not enabled when it is not configured. For complete fields and default values, see [KV Pooling README — kv_cache_store_config](../features/kv_cache_store/README.md); for the Mooncake `standalone` deployment mode, see [Mooncake backend documentation](../features/kv_cache_store/backend/mooncake.md#scenario-2-standalone-mode-configuration). |

In PD mode, **configure `health_check_config` independently** for P and D, and the code default values are used when not configured.

### vLLM Startup Acceleration

`vllm_startup_acceleration_config` applies only to the engine configuration whose `engine_type` is `vllm`.

- `enable_startup_plan=true`: enables vLLM StartPlan. After the profile is hit and passes the security check, memory profiling is skipped.
- `enable_startup_plan=false`: no StartPlan is generated or loaded.
- `enable_startup_plan=true` requires that the matched vLLM/vLLM-Ascend version has integrated StartPlan; Motor does not modify the runtime source code.
- `enable_graph_reuse=true`: enables full graph reuse of the vLLM-Ascend backend, and sets `enable_npugraph_ex=true`, `enforce_eager=false`,
  and the `cudagraph_mode` corresponding to the role based on this configuration.
- `enable_graph_reuse=false`: disables full graph reuse of the vLLM-Ascend backend, and does not forcibly disable normal graph capture or the AOT cache.
- `cache_root`: root directory of the vLLM cache, used to store the StartPlan, AOT, and backend compiled graph caches. In PD disaggregation scenarios, it is recommended to configure Prefill and
  Decode with directories that are separately persistent and readable and writable by the vLLM process.

### dispatch_profile

When `engine_config.kv_transfer_config.kv_connector` is not in the built-in identification whitelist, the P/D collaborative semantics can be explicitly declared at the top level of `motor_engine_prefill_config` / `motor_engine_decode_config`. NodeManager derives compatible metadata from this field and validates its semantics when constructing the native vLLM startup command.

| Configuration Item | Type | Description |
|--------|------|------|
| dispatch_profile | string | The optional values are `handoff` or `trigger`. Native vLLM P/D accepts both: `handoff` means that Prefill hands over to Decode after completion; `trigger` means that Decode starts first and triggers Prefill through the Worker metaserver (`MooncakeLayerwiseConnector` is automatically derived as `trigger`). SGLang uses its own bootstrap protocol. The Prefill and Decode ends should be consistent. |

>[!NOTE]NOTE
> `dispatch_profile` is written at the top level of `motor_engine_*_config`, not inside `engine_config`. `dispatch_capabilities` is an internal compatibility field and does not support direct user configuration.

### health_check_config

Optional virtual inference health probe configuration, located in the `motor_engine_prefill_config` / `motor_engine_decode_config` module. It is disabled by default. For the mechanism description, see [Virtual Inference Health Probe](../features/sim_inference.md).

**Table 7** `health_check_config` field parameter description

| Configuration Item | Type | Description |
|--------|------|------|
| enable_virtual_inference | bool | Switch for Motor-initiated virtual inference, default value: false. When set to `true`, periodic virtual inference starts after the inference plane `/health` becomes READY for the first time. It takes effect only for DP rank 0 of vLLM and non-headless endpoints; setting it to `false` does not disable the native generative `GET /health` of SGLang. It can be enabled only when the final engine environment `ASCEND_GLOBAL_LOG_LEVEL=3` (ERROR by default when not configured). |
| npu_usage_threshold | int | AI Cube utilization threshold (%), default value: 3. Used only by vLLM Motor virtual inference, and `0 < npu_usage_threshold <= 100` must be satisfied. |
| max_failure_count | int | Upper limit of consecutive virtual inference failures, default value: 6. Used only by vLLM Motor virtual inference; after the threshold is reached, the endpoint is degraded to ABNORMAL, but process restart is not triggered. |
| virtual_inference_timeout | float | Timeout of the periodic proactive virtual inference request (seconds), default value: 5.0, which must be a positive number. It takes effect only for vLLM Motor virtual inference; the first warmup request is fixed at 180 seconds. |
| health_collector_timeout | int | Probe timeout of the inference plane `GET /health` (seconds), default value: 5. It is used by both vLLM and SGLang heartbeats. |
| health_collector_timeout_retry_attempts | int | Maximum number of attempts after a single `GET /health` timeout (including the first request), default value: 3; retries occur only on timeout. |
| startup_timeout | int | Startup window for native engine model loading (seconds), default value: 1800. Within the window, connection failures keep the state as STARTING and the instance is not determined to be abnormal. |

## Other Parameter Descriptions

### motor_engine_union_env Field

In the PD aggregation scenario, the environment variables of the union native engine are configured in `motor_engine_union_env` of `env.json`. For an example, see `model_configs/vllm/pd_hybrid/env.json`.

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

<a id="prefill_kv_event_config-automatic-derivation"></a>

### kv_conductor_config Automatic Derivation and Compatibility

The runtime entry of the Coordinator is `motor_coordinator_config.scheduler_config.kv_conductor_config`.
The recommended configuration for complete deployment is to configure `kv_conductor_config` at the top level of `user_config.json`, which is merged by the loader.
The Deployer reads only the top-level configuration to determine whether the conductor service is enabled and the service port, and the nested configuration cannot replace it.

It is recommended to write the same field in only one place. The current merge fills in values in the order of nested configuration → top-level configuration → legacy field → engine derivation;
existing non-empty and non-zero values are retained, while empty values or 0 are filled in from subsequent sources, and override relationships should not be expressed by repeated cross-layer configuration.
The migration of the legacy `prefill_kv_event_config` covers only `conductor_service`, `http_server_port`, `engine_type`,
`model_path`, `block_size`, `endpoint`, `replay_endpoint`, and `re_register_interval_sec`,
and does not include new fields such as `query_encoding` and media ports.
The Coordinator automatically identifies the P/D disaggregation or union aggregation topology based on the instance role, and selects the vLLM handoff or SGLang bootstrap Adapter based on `engine_type`. For details about the vLLM Connector whitelist, the rule that `MultiConnector` takes `connectors[0]`, and the fail-closed handling of unknown Connectors during startup.

**Table 9** Sources of automatic derivation of `kv_conductor_config`

| Source | Description |
|------|------|
| PD disaggregation | `motor_engine_prefill_config` is read preferentially. The event configuration provides `endpoint` / `replay_endpoint`, and the model configuration provides `model_path`. |
| PD aggregation | When there is no Prefill section, `motor_engine_union_config` is read, with the same derived fields; the Decode section is not used for derivation.|
| block_size | When not explicitly configured, it is derived from the `block-size` of the selected engine section, defaulting to `128`; for hybrid KV, the actual event granularity of the main group must be verified and explicitly overridden. |
| kv_conductor_config | The top-level field is merged into `scheduler_config.kv_conductor_config`; the HTTP port defaults to `13333`, and the deployment enabling condition still requires explicit configuration. |
