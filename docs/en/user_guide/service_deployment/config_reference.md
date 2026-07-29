# Parameters in `user_config`

This document describes all configurable items of components such as Controller, Coordinator, and NodeManager in `user_config.json`, which correspond to the `examples/features/config_sample.json` structure. During deployment, the corresponding modules in `user_config.json` are merged into the component runtime configuration. The default values in the code are used first, and then the user configuration overwrites the default values. The configuration file monitored by a component can be modified to make the modification take effect dynamically. The configuration file is stored in the `examples/infer_engines/` directory (for example, `examples/infer_engines/vllm/user_config.json`). Select the configuration based on the engine type and model.

## 1. `motor_deploy_config` (Deployment and Resources)

`motor_deploy_config` contains deployment and resource configurations, which are read by `deploy.py` to generate Kubernetes resources and inject environment variables.

**Configuration example**:

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
  "deploy_mode": "infer_service_set",
  "tls_config": { ... }
}
```

| Configuration Item| Type| Description|
|--------|------|------|
| p_instances_num | int | Number of P instances, ≥ 1 and ≤ 16.|
| d_instances_num | int | Number of D instances, ≥ 1 and ≤ 16.|
| single_p_instance_pod_num | int | Number of pods corresponding to a single P instance, ≥ 1.|
| single_d_instance_pod_num | int | Number of pods corresponding to a single D instance, ≥ 1.|
| p_pod_npu_num | int | Number of NPUs occupied by a single P instance pod. Each pod supports a maximum of 16 NPUs.|
| d_pod_npu_num | int | Number of NPUs occupied by a single D instance pod. Each pod supports a maximum of 16 NPUs.|
| image_name | string | Inference image name (including the running environments such as MindIE-PyMotor and vLLM). The name must be the same as that of the image prepared or loaded in [PD Disaggregation Deployment](./pd_disaggregation_deployment.md#preparing-an-image).|
| job_id | string | Deployment task name, which is also used as the Kubernetes namespace, for example, `mindie-motor`.|
| hardware_type | string | Hardware type: `800I_A2` or `800I_A3`|
| weight_mount_path | string | Path for mounting the model weight on the host machine. The value of `model_path` in the container must be the same as the mount path, for example, `"/mnt/weight/"`.|
| deploy_mode | string | Deployment mode. Options: `infer_service_set` (default, based on the InferServiceSet CRD, a single `infer_service.yaml` file is generated and the CRD controller starts each pod); `multi_deployment` (traditional mode, multiple independent YAML files such as `controller`, `coordinator`, `engine_*`, and `kv_pool` are generated and applied separately); `single_container` (single-container mode, P/D combined running). If this parameter is not set, the default value `infer_service_set` is used. The adaptation verification of the RAS capability and pooling capability has not been completed in CRD mode. If the reliability, availability, and serviceability (RAS) or KV pooling capability is required, set this parameter to `multi_deployment`.|
| tls_config | object | (Optional) TLS configuration, including `infer_tls_config`, `mgmt_tls_config`, `etcd_tls_config`, and `grpc_tls_config`.|

---

## 2. `motor_controller_config`

The configuration of `motor_controller_config` in `examples/features/config_sample.json` is as follows:

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

### 2.1 `logging_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| log_level | string | Log level. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, and more. Default value: `INFO`|
| log_max_line_length | int | Maximum length of a log; exceeding this will result in truncation. Default value: `8192`|
| log_file | string/null | Path to the log output file. If the value is `null`, the log is output to the standard output. Default value: `null`|
| log_format | string | Log format template, which supports Python logging placeholders. Default value: `%(asctime)s [%(levelname)s][%(name)s][%(filename)s:%(lineno)d] %(message)s`|
| log_date_format | string | Log date format, for example, `%Y-%m-%d %H:%M:%S`. Default value: `%Y-%m-%d %H:%M:%S`|

If the preceding default format is used, a log example is as follows:

```txt
2026-02-12 14:30:00  [INFO][motor.coordinator][main.py:42]  Service started.
2026-02-12 14:30:01  [WARNING][motor.engine_server][service.py:128]  Retry connection to etcd.
2026-02-12 14:30:02  [ERROR][motor.controller][controller_api.py:56]  Request failed: connection timeout.
```

### 2.2 `api_config`

| Configuration Item                       | Type| Description                                                             |
|----------------------------|------|-----------------------------------------------------------------|
| controller_api_host        | string | Controller API listening address (IP address or host name). Default value: `127.0.0.1` (or `Env.pod_ip`)      |
| controller_api_port        | int | Controller API port. Default value: `1026`                                    |

### 2.3 `mgmt_tls_config`/`etcd_tls_config`/`grpc_tls_config`

The configuration structures of the three types of TLS are the same. The fields are as follows.

| Configuration Item| Type| Description|
|--------|------|------------------|
| tls_enable | bool | Enable TLS. Options: `true`/`false`. Default value: `true`|
| ca_file | string | Path to the CA certificate. Default value: `security/mgmt/cert/ca.crt`|
| cert_file | string | Path to the certificate file on the server. Default value: `security/mgmt/cert/server.crt`|
| key_file | string | Path to the private key file. Default value: `security/mgmt/keys/server.key`|
| passwd_file | string | Path to the password file used for decrypting the private key. Default value: `security/mgmt/keys/key_pwd.txt`|
| crl_file | string | Path to the certificate revocation list (CRL) file. This parameter is optional. An empty string indicates that the CRL file is not used. Default value: `""`|

### 2.4 `instance_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| instance_assemble_timeout | int | Maximum waiting time for an instance to be ready, in seconds. Default value: `600`|
| instance_assembler_check_internal | int | Interval for polling the instance assembly status, in seconds. Default value: `1`|
| instance_assembler_cmd_send_internal | int | Interval for delivering assembly commands to instances, in seconds. Default value: `1`|
| instance_manager_check_internal | int | Interval for checking the instance status, in seconds. Default value: `1`|
| instance_heartbeat_timeout | int | Duration in seconds after which an instance is considered unhealthy if no heartbeat is received. Default value: `5`|
| instance_expired_timeout | int | Duration in seconds after which an idle instance is cleared. Default value: `300`|
| send_cmd_retry_times | int | Number of retry attempts when a command fails to be delivered to an instance. Default value: `3`|

### 2.5 `event_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| event_consumer_sleep_interval | float | Interval in seconds between event queue polls, that is, the waiting time (in seconds) after an event is processed. Default value: `1.0`|
| coordinator_heartbeat_interval | float | Interval for reporting heartbeat between the Controller and Coordinator, in seconds. Default value: `5.0`|

### 2.6 `fault_tolerance_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| enable_fault_tolerance | bool | Whether to enable fault self-healing (advanced RAS). Options: `true`/`false`. Default value: `false`|
| strategy_center_check_internal | int | Interval for polling the policy center, in seconds. Default value: `1`|
| enable_scale_p2d | bool | Whether to enable P2D elastic scaling. Options: `true`/`false`. Default value: `false`|
| enable_lingqu_network_recover | bool | Whether to enable UB network fault recovery. Options: `true`/`false`. Default value: `false`|

### 2.7 `standby_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| enable_master_standby | bool | Whether to enable the active/standby mode for the Controller. Options: `true`/`false`. Default value: `false`|
| master_standby_check_interval | int | Interval for detecting the master/standby role, in seconds. Default value: `5`|
| master_lock_ttl | int | Lease duration (in seconds) for the master node's lock on etcd. Default value: `10`|
| master_lock_retry_interval | int | Retry interval (in seconds) for acquiring the lock during master election. Default value: `5`|
| master_lock_max_failures | int | Maximum consecutive failures allowed when competing for master lock before abandoning and switching. Default value: `3`|
| master_lock_key | string | Lock path of the master node in etcd. The prefix `/controller/` is automatically added during running. Default value: `/master_lock` (actually `/controller/master_lock`)|

### 2.8 `etcd_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| etcd_host | string | etcd service address (host name or IP address). Default value: `etcd.default.svc.cluster.local`|
| etcd_port | int | etcd port number. Default value: `2379`|
| etcd_timeout | int | etcd operation timeout interval (in seconds). Default value: `5`|
| etcd_ca_cert | string/null | Path to the etcd CA certificate. This parameter is optional. The value `null` indicates that the CA certificate is not used. Default value: `null`|
| etcd_cert_key | string/null | Path to the etcd client private key. This parameter is optional. Default value: `null`|
| etcd_cert_cert | string/null | Path to the etcd client certificate. This parameter is optional. Default value: `null`|
| enable_etcd_persistence | bool | Whether to enable etcd persistence. Options: `true`/`false`. Default value: `false`|

---

## 3. `motor_coordinator_config`

The configuration of `motor_coordinator_config` in `examples/features/config_sample.json` is as follows:

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
    "deploy_mode": "pd_separate",
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

### 3.1 `logging_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| log_level | string | Log level. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, and more. Default value: `INFO`|
| log_max_line_length | int | Maximum length of a log line; exceeding this will result in truncation. Default value: `8192`|
| log_file | string/null | Log file path. If the value is `null`, the log is output to the standard output. Default value: `null`|
| log_format | string | Log format template, which supports Python logging placeholders. Default value: `%(asctime)s [%(levelname)s][%(name)s][%(filename)s:%(lineno)d] %(message)s`|
| log_date_format | string | Log date format. Default value: `%Y-%m-%d %H:%M:%S`|

### 3.2 `prometheus_metrics_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| reuse_time | int | Cache reuse duration of Prometheus metrics, in seconds. Default value: `3`|

### 3.3 `exception_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| max_retry | int | Maximum number of retries after a request fails. Default value: `5`|
| recompute_enabled | bool | Whether to allow the Coordinator to recalculate the token cache and retry when the engine returns `stop_reason=recomputed`. Default value: `true`|
| retry_delay | float | Waiting time before each retry, in seconds. Default value: `0.2`|
| first_token_timeout | int | Timeout interval for waiting for the first token, in seconds. Default value: `600`|
| infer_timeout | int | Total timeout interval for a single inference request, in seconds. Default value: `3600`|

### 3.4 `scheduler_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| deploy_mode | string | Deployment mode. <ul><li>`pd_separate`: PD disaggregation deployment;</li><li>`cdp_separate`: CDP deployment;</li><li>`cpcd_separate`: CPCD deployment. </li></ul>Default value: `pd_separate`|
| scheduler_type | string | Scheduling type. <ul><li>`load_balance`: load balancing;</li><li>`round_robin`: round-robin. </li></ul>Default value: `load_balance`|

### 3.5 `infer_tls_config`/`mgmt_tls_config`/`etcd_tls_config`

The configuration structures of the three types of TLS are the same. The fields are as follows.

| Configuration Item| Type| Description|
|--------|------|------------------|
| tls_enable | bool | Enable TLS. Options: `true`/`false`. Default value: `true`|
| ca_file | string | Path to the CA certificate. Default value: `security/mgmt/cert/ca.crt`|
| cert_file | string | Path to the certificate file on the server. Default value: `security/mgmt/cert/server.crt`|
| key_file | string | Path to the private key file. Default value: `security/mgmt/keys/server.key`|
| passwd_file | string | Path to the password file used for decrypting the private key. Default value: `security/mgmt/keys/key_pwd.txt`|
| crl_file | string | Path to the CRL file. This parameter is optional. An empty string indicates that the CRL file is not used. Default value: `""`|

### 3.6 `timeout_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| request_timeout | int | Timeout interval for a single HTTP request, in seconds. Default value: `30`|
| connection_timeout | int | Timeout interval for establishing a connection, in seconds. Default value: `10`|
| read_timeout | int | Timeout interval for read operations, in seconds. Default value: `15`|
| write_timeout | int | Timeout interval for write operations, in seconds. Default value: `15`|
| keep_alive_timeout | int | Connection keepalive duration, in seconds. If no activity is detected within this duration, the connection is closed. Default value: `60`|

### 3.7 `api_key_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| enable_api_key | bool | Whether to enable API key authentication. Options: `true`/`false`. Default value: `false`|
| valid_keys | array | List of valid API key strings. Default value: `[]`|
| encryption_algorithm | string | Encryption algorithm used for key verification, for example, `PBKDF2_SHA256`. Default value: `PBKDF2_SHA256`|
| header_name | string | Name of the HTTP header that carries the API key. Default value: `Authorization`|
| key_prefix | string | Prefix of the key in the header, for example, `Bearer`. Default value: `Bearer`|
| skip_paths | array | List of paths (such as `/metrics`, `/liveness` and `/docs`) where the API key is not verified. This can be customized.|

### 3.8 `rate_limit_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| enable_rate_limit | bool | Whether to enable request throttling. Options: `true`/`false`. Default value: `false`|
| max_requests | int | Maximum number of requests allowed in the throttling time window. Default value: `1000`|
| window_size | int | Length of the time window for collecting throttling statistics, in seconds. Default value: `60`|
| scope | string | Scope where throttling takes effect, for example, `global`. Default value: `global`|
| skip_paths | array | List of paths (such as `/liveness`, `/readiness` and `/metrics`) that are not involved in throttling statistics. This can be customized.|
| error_message | string | Message returned to the client when throttling is triggered. Default value: `too many requests, please try again later`|
| error_status_code | int | HTTP status code returned when throttling is triggered. Generally, the value is `4xx` (for example, `429`). Default value: `429`|

### 3.9 `standby_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| enable_master_standby | bool | Whether to enable the active/standby mode for the Coordinator. Options: `true`/`false`. Default value: `false`|
| master_standby_check_interval | int | Interval for detecting the master/standby role, in seconds. Default value: `5`|
| master_lock_ttl | int | Lease duration (in seconds) for the master node's lock on etcd. Default value: `10`|
| master_lock_retry_interval | int | Retry interval (in seconds) for acquiring the lock during master election. Default value: `5`|
| master_lock_max_failures | int | Maximum consecutive failures allowed when competing for master lock before abandoning and switching. Default value: `3`|
| master_lock_key | string | Lock path of the master node in etcd. The prefix `/coordinator/` is automatically added during running. Default value: `/master_lock` (actually `/coordinator/master_lock`)|

### 3.10 `etcd_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| etcd_host | string | etcd service address (host name or IP address). Default value: `etcd.default.svc.cluster.local`|
| etcd_port | int | etcd port number Default value: `2379`|
| etcd_timeout | int | etcd operation timeout interval (in seconds). Default value: `5`|
| enable_etcd_persistence | bool | Whether to enable etcd persistence. Options: `true`/`false`. Default value: `false`|
| tls_config | object | TLS of the etcd client, which is optional. Sub-fields: `enable_tls` (`true`/`false`), `ca_cert`, `tls_cert`, `tls_key`, and `tls_passwd`|

### 3.11 `api_config`

| Configuration Item                       | Type| Description                                                        |
|----------------------------|------|------------------------------------------------------------|
| coordinator_api_host       | string | Coordinator API listening address (IP address or host name). Default value: `127.0.0.1` (or `Env.pod_ip`)|
| coordinator_api_dns        | string | Domain name.                                                        |
| coordinator_api_infer_port | int | Inference plane port. Default value: `1025`                                           |
| coordinator_api_mgmt_port  | int | Management plane port. Default value: `1026`                                           |

### 3.12 `request_limit` (Commonly Used in `user_config`)

It is not included in `config_sample.json` but is commonly used during PD deployment. It takes effect after being merged into the runtime configuration.

| Configuration Item| Type| Description|
|--------|------|------------------|
| single_node_max_requests | int | Maximum number of concurrent requests allowed by a single node, which is configured in `user_config`.|
| max_requests | int | Maximum number of concurrent requests in the cluster, which is configured in `user_config`.|

### 3.13 `aigw_model`

`aigw_model` is a centralized configuration of AIGW model metadata and is used for model information returned by APIs such as `/v1/models`. In the `user_config.json` file, this parameter corresponds to the **`aigw`** object under `motor_coordinator_config`. If this parameter is not used, the value is `null`. The internal configurable items are as follows.

| Configuration Item| Type| Description|
|--------|------|------------------|
| id | string | Model ID, which is the same as the model name in the OpenAI-compatible API. If `model_name` of Prefill/Decode is configured, it is automatically filled as the `model_name` of Prefill during deployment.|
| object | string | Object type. The value is fixed to `model`. If this parameter is not set during deployment, it is automatically filled.|
| owned_by | string | Model owner, for example, `motor`. If this parameter is not set during deployment, it is automatically filled.|
| p_max_seqlen | int | Maximum sequence length on the Prefill end (positive integer). If this parameter is not set, it is automatically filled from the Prefill `engine_config.max_model_len`.|
| d_max_seqlen | int | Maximum sequence length on the Decode end (positive integer). If this parameter is not set, it is automatically filled from the Decode `engine_config.max_model_len`.|
| slo_ttft | int | TTFT SLO (ms), used for scheduling/monitoring. Default value: `1000`|
| slo_tpot | int | TPOT SLO (ms), used for scheduling/monitoring. Default value: `50`|

---

## 4. `motor_nodemanger_config`

The configuration of `motor_nodemanger_config` in `examples/features/config_sample.json` is as follows:

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
    "heartbeat_interval_seconds": 1,
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

### 4.1 `api_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| pod_ip | string | Pod IP address (injected by the environment or deployment). Default value: `127.0.0.1` (or `Env.pod_ip`)|
| node_manager_port | int | NodeManager port. Default value: `1026`|
| controller_api_dns | string | Controller API domain name or IP address, which is usually injected by the deployment or environment. Default value: `127.0.0.1`|
| controller_api_port | int | Controller API port. Default value: `1026`|

### 4.2 `mgmt_tls_config`

The TLS configuration structure is the same as that in section 2.3. The fields are as follows.

| Configuration Item| Type| Description|
|--------|------|------------------|
| tls_enable | bool | Enable TLS. Options: `true`/`false`. Default value: `true`|
| ca_file | string | Path to the CA certificate. Default value: `security/mgmt/cert/ca.crt`|
| cert_file | string | Path to the certificate file on the server. Default value: `security/mgmt/cert/server.crt`|
| key_file | string | Path to the private key file. Default value: `security/mgmt/keys/server.key`|
| passwd_file | string | Path to the password file used for decrypting the private key. Default value: `security/mgmt/keys/key_pwd.txt`|
| crl_file | string | Path to the CRL file. This parameter is optional. An empty string indicates that the CRL file is not used. Default value: `""`|

### 4.3 `endpoint_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| endpoint_num | int | Number of engine endpoints, which is usually deduced from the HCCL/parallel configuration. Default value: `0`|
| base_port | int | Start number of the endpoint port. Default value: `10000`|
| mgmt_ports | array | List of control ports of each endpoint (integer array). Default value: `[]`|
| service_ports | array | List of inference service ports of each endpoint (integer array). Default value: `[]`|

### 4.4 `basic_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| job_name | string/null | Task or job name, which is usually injected by the environment or deployment. Default value: `Env.job_name` or `null`|
| role | string | Role of the current node. Options: `prefill` (prefilling only), `decode` (decoding only), and `both` (prefilling + decoding). Default value: `both`|
| model_name | string | Model name, which is injected by `user_config` during PD deployment. Default value: `""`|
| hardware_type | string | Hardware model, for example, `800I-A3`. Default value: `800I-A3`|
| heartbeat_interval_seconds | int | Interval for reporting heartbeat to the Controller, in seconds. Default value: `1`|
| device_num | int | Number of NPU devices, which is usually deduced from the HCCL configuration. Default value: `0`|
| parallel_config | object | Parallel dimension configuration. For details, see the following table. Default value: `1` for each dimension. The value of `world_size` is automatically calculated by the system based on each dimension.|

**parallel_config subfields**:

| Configuration Item| Type| Description|
|--------|------|------------------|
| dp_size | int | Data parallelism. Default value: `1`|
| cp_size | int | Context parallelism. Default value: `1`|
| tp_size | int | Tensor parallelism. Default value: `1`|
| sp_size | int | Sequence parallelism. Default value: `1`|
| ep_size | int | Expert parallelism. Default value: `1`|
| pp_size | int | Pipeline parallelism. Default value: `1`|
| world_size | int | Total number of processes. If the value is `0`, the system automatically calculates the value based on the formula: dp × cp × tp × pp. Default value: **0**|

### 4.5 `logging_config`

| Configuration Item| Type| Description|
|--------|------|------------------|
| log_level | string | Log level. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, and more. Default value: `INFO`|
| log_max_line_length | int | Maximum length of a log; exceeding this will result in truncation. Default value: `8192`|
| log_file | string/null | Path to the log output file. If the value is `null`, the log is output to the standard output. Default value: `null`|
| log_format | string | Log format template, which supports Python logging placeholders. Default value: `%(asctime)s [%(levelname)s][%(name)s][%(filename)s:%(lineno)d] %(message)s`|
| log_date_format | string | Log date format. Default value: `%Y-%m-%d %H:%M:%S`|
