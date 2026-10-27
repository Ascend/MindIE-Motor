# Interface Description

MindIE Motor provides the inference [Business Interface](#business-interface), [Management Interface](#management-interface), [Metrics Interface](#metrics-interface), and [Observation Interface](#observation-interface).

## Business Interface

MindIE Motor provides the following inference service interfaces:

- [OpenAI Chat Completion Interface](./service_interfaces.md#openai-chat-completion-interface): `/v1/chat/completions`

- [OpenAI Responses Interface](./service_interfaces.md#openai-responses-interface): `/v1/responses`

- [OpenAI Completion Interface](./service_interfaces.md#openai-completion-interface): `/v1/completions`

- [Anthropic Messages Interface](./service_interfaces.md#anthropic-messages-interface): `/v1/messages`

- [Anthropic Count Tokens Interface](./service_interfaces.md#anthropic-count-tokens-interface): `/v1/messages/count_tokens`

- [Model List Query Interface](./service_interfaces.md#model-list-query-interface): `/v1/models`

### IP/Port and Configuration of the Business Interface

**Inference Service Interface IP**

- When deployed using Kubernetes, the Inference Service interface IP uses the host IP or domain name.

- Within the Kubernetes cluster, the Inference Service interface IP uses the IP of the `Coordinator` service.

  - The value is obtained from the `coordinator_api_host` configuration item in the [user_config.json](../configuration/config_reference.md#motor_coordinator_config) configuration file.

  - When this configuration item is absent from the configuration file, the environment variable `POD_IP` of the `Coordinator` service deployment is used.

  - When the environment variable `POD_IP` is also absent or empty, the default value `127.0.0.1` is used.

**Inference Service Interface Port**

- When deployed using Kubernetes, the Inference Service interface port uses the `nodePort` defined in the `mindie-motor-coordinator-infer` metadata in the `yaml` file, with a default value of `31015`.

  - When deployed in CRD mode, refer to [examples/deployer/yaml_template/infer_service_template.yaml](../../../../examples/deployer/yaml_template/infer_service_template.yaml) for the `yaml` file;

  - When deploying in Multi mode, refer to [examples/deployer/yaml_template/coordinator_template.yaml](../../../../examples/deployer/yaml_template/coordinator_template.yaml) for the `yaml` file.

- In a Kubernetes cluster, the Inference Service interface port uses the port defined by `coordinator_api_infer_port` in the [user_config.json](../configuration/config_reference.md#motor_coordinator_config) configuration file.

  - When this configuration item is absent from the configuration file, the default port `1025` is used.

## Management Interface

MindIE Motor provides the following management interfaces:

- [Startup Probe Interface](./management_interfaces.md#startup-probe-interface): `/startup`

- [Liveness Probe Interface](./management_interfaces.md#liveness-probe-interface): `/liveness`

- [Readiness Probe Interface](./management_interfaces.md#readiness-probe-interface): `/readiness`

- [Instance Query Interface](./management_interfaces.md#instance-query-interface): `/instances`

- [Instance Refresh Interface](./management_interfaces.md#instance-refresh-interface): `/instances/refresh`

- [Precision Alarm State Clearing Interface](./management_interfaces.md#precision-alarm-state-clearing-interface): `/precision/alarm_cleared`

- [Root Path Service Information Interface](./management_interfaces.md#root-path-service-information-interface): `/`

- [Health Status Query Interface](./management_interfaces.md#health-status-query-interface): `/health`

>[!NOTE]NOTE
>
> - Except for instance query, the management interfaces are intended for use only within the Kubernetes cluster and are not exposed outside the cluster.
> - `/instances` is also retained on the internal management port `1026` and is exposed for read-only queries through the Coordinator Observability port `1027` (K8s nodePort `31017`).
> - `/health` and `/metrics` are mounted on the Coordinator Observability port (`coordinator_obs_port`, default `1027`), **not** on the management interface port (`coordinator_api_mgmt_port`, default `1026`). For details, see [Management Interface](./management_interfaces.md#health-status-query-interface).

### IP/Port and Configuration of the Management Interface

**Management Interface IP**

- In the Kubernetes cluster, the management interface IP uses the IP of the `Coordinator` service.

  - The value comes from the `coordinator_api_host` configuration item in the [user_config.json](../configuration/config_reference.md#motor_coordinator_config) configuration file.

  - When this configuration item is absent from the configuration file, the environment variable `POD_IP` of the `Coordinator` service deployment is used.

  - When the environment variable `POD_IP` does not exist or is empty, the default value `127.0.0.1` is used.

**Management Interface Port**

- In the Kubernetes cluster, the management interface port uses the port defined by `coordinator_api_mgmt_port` in the [user_config.json](../configuration/config_reference.md#motor_coordinator_config) configuration file.

  - When this configuration item is absent from the configuration file, the default interface port `1026` is used.

## Metrics Interface

The monitoring metrics of MindIE Motor are centrally collected and aggregated by the Coordinator and exposed externally through the Coordinator Observability port. The metrics interface is **the only recommended way to obtain metrics**:

- [Metrics Query Interface](./metrics_interfaces.md#interface-format): `/metrics`, which supports aggregated views (`full`/`instance`/`role`/`dp`/`node`) and return formats (Prometheus / OpenTelemetry)

- [Instance Query Interface](./management_interfaces.md#instance-query-interface): `/instances`, which allows ras_monitor or external O&M tools to obtain the instance health status

>[!NOTE]NOTE
>
> - Metrics are collected by the Coordinator from each Engine periodically (default `3` seconds) and semantically aggregated. For details, see [Metrics Interface](./metrics_interfaces.md).
> - The `/observability/metrics` on the Controller side is a deprecated forwarding interface. **For new integrations, use this port directly**. For details, see [Observation Interface](#observation-interface).

### IP/Port and Configuration of the Metrics Interface

**Metrics Interface IP**

- When deployed using Kubernetes, the Metrics Interface IP uses the host IP or domain name.

- In the Kubernetes cluster, the Metrics Interface IP uses the IP of the `Coordinator` service.

  - The value comes from the `coordinator_api_host` configuration item in the [user_config.json](../configuration/config_reference.md#motor_coordinator_config) configuration file.

  - When this configuration item is absent from the configuration file, the environment variable `POD_IP` of the `Coordinator` service deployment is used.

  - When the environment variable `POD_IP` does not exist or is empty, the default value `127.0.0.1` is used.

**Metrics Interface Port**

- When deployed using Kubernetes, the Metrics Interface Port uses the `nodePort` defined in the `mindie-motor-coordinator-obs` metadata in the `yaml` file, with a default value of `31017`.

  - When deployed in CRD mode, refer to [examples/deployer/yaml_template/infer_service_template.yaml](../../../../examples/deployer/yaml_template/infer_service_template.yaml) for the `yaml` file;

  - When deploying in Multi mode, refer to [examples/deployer/yaml_template/coordinator_template.yaml](../../../../examples/deployer/yaml_template/coordinator_template.yaml) for the `yaml` file.

- In a Kubernetes cluster, the Metrics Interface port uses the port defined by `coordinator_obs_port` in the [user_config.json](../configuration/config_reference.md#motor_coordinator_config) configuration file.

  - When this configuration item is absent from the configuration file, the default port `1027` is used.

- The TLS of the Metrics Interface is controlled by `mgmt_tls_config.enable_tls`.

>[!NOTE]NOTE
>The Metrics Interface port (`coordinator_obs_port`, default `1027`) and the Controller Observation Interface port (`observability_api_port`, default `1027`) share the same default value, but they belong to different components and different services: the Metrics Interface is located on the `Coordinator` (nodePort `31017`), while the Observation Interface is located on the `Controller` (nodePort `31027`). Be sure to distinguish between them when using.

## Observation Interface

The Controller observation interface provides O&M observation data such as the model service inventory and alarms (**excluding metrics**; for metrics, use the [Metrics Interface](#metrics-interface)):

- [Model Service Inventory Query Interface](./observability_interface.md#model-service-inventory-query-interface): `/observability/inventory`

- [Alarm Query Interface](./observability_interface.md#alarm-query-interface): `/observability/alarms`

- [Integrating with the CCAE Frontend Platform](./observability_interface.md#connecting-to-the-ccae-frontend-platform)

>[!NOTE]NOTE
>
> - `/observability/metrics` is deprecated: this interface is a proxy that forwards to the Coordinator `/metrics` and is retained only for compatibility. For new integrations, use the [Metrics Interface](#metrics-interface) directly.
> - The observation interface is disabled by default and takes effect only after `observability_config.observability_enable=true` is configured. In primary/standby mode, only the primary Controller provides query capabilities externally.

### IP/Port and Configuration of the Observation Interface

**Observation Interface IP**

- When deployed using Kubernetes, the Observation Interface IP uses the host IP or domain name.

- Within the Kubernetes cluster, the Observation Interface IP uses the IP of the `Controller` service.

  - The value comes from the `controller_api_host` configuration item in the [user_config.json](../configuration/config_reference.md#motor_controller_config) configuration file.

  - When this configuration item is absent from the configuration file, the environment variable `POD_IP` of the `Controller` service deployment is used.

  - When the environment variable `POD_IP` is also absent or empty, the default value `127.0.0.1` is used.

**Observation Interface Port**

- When deployed using Kubernetes, the Observation Interface port uses the `nodePort` defined in the `mindie-motor-observability` metadata in the `yaml` file, with a default value of `31027`.

  - When deployed in CRD mode, refer to [examples/deployer/yaml_template/infer_service_template.yaml](../../../../examples/deployer/yaml_template/infer_service_template.yaml) for the `yaml` file;

  - When deploying in Multi mode, refer to [examples/deployer/yaml_template/controller_template.yaml](../../../../examples/deployer/yaml_template/controller_template.yaml) for the `yaml` file.

- In a Kubernetes cluster, the Observation Interface port uses the port defined by `observability_api_port` in the [user_config.json](../configuration/config_reference.md#motor_controller_config) configuration file.

  - When this configuration item is absent from the configuration file, the default port `1027` is used.

## Security, Authentication, and Rate Limiting

- Security protocol: When `infer_tls_config.enable_tls` / `mgmt_tls_config.enable_tls` is `true`, the inference/management interface ports use `https`

- Management-plane authentication: When `mgmt_api_key_config.enable_api_key=true`, the instance query, instance refresh, and precision alarm state clearing interfaces must carry `X-Motor-Management-Key`; probe interfaces are exempt from authentication. The key is provided by the file specified by `api_key_file`

- Request headers:

  - Required: `Content-Type: application/json`

  - Optional: API Key

    - Takes effect on `/v1/completions`, `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, and `/v1/messages/count_tokens`

    - Header name: `api_key_config.header_name` (default `Authorization`)

    - Prefix: `api_key_config.key_prefix` (default `Bearer`)

- Rate limiting (optional): Enabled when `rate_limit_config.enable_rate_limit=true`; returns `429` when the limit is exceeded. For the mechanism and configuration, see [rate limiting](../features/rate_limiting.md)
