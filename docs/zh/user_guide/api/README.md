# 接口说明

MindIE Motor提供推理[业务接口](#业务接口)、[管理接口](#管理接口)、[指标接口](#指标接口)和[观测接口](#观测接口)。

## 业务接口

MindIE Motor提供下列推理业务接口：

- [OpenAI Chat Completion 接口](./service_interfaces.md#openai-chat-completion-接口)：`/v1/chat/completions`
- [OpenAI Completion 接口](./service_interfaces.md#openai-completion-接口)：`/v1/completions`
- [Anthropic Messages 接口](./service_interfaces.md#anthropic-messages-接口)：`/v1/messages`
- [Anthropic Count Tokens 接口](./service_interfaces.md#anthropic-count-tokens-接口)：`/v1/messages/count_tokens`
- [模型列表查询接口](./service_interfaces.md#模型列表查询接口)：`/v1/models`

### 业务接口的IP/端口与配置

**推理业务接口IP**

- 使用Kubernetes部署时，推理业务接口IP使用主机IP或者域名。
- 在Kubernetes集群内，推理业务接口IP使用`Coordinator`服务的IP。
  - 取值来自于[`user_config.json`](../configuration/config_reference.md#motor_coordinator_config)配置文件中的`coordinator_api_host`配置项。
  - 当配置文件中无此配置项时，则使用`Coordinator`服务部署的环境变量`POD_IP`。
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**推理业务接口端口**

- 使用Kubernetes部署时，推理业务接口端口使用`yaml`文件中`mindie-motor-coordinator-infer`元数据定义的`nodePort`，默认值为`31015`。
  - 当使用CRD模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)；
  - 当使用Multi模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/coordinator_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/coordinator_template.yaml)。
- 在Kubernetes集群内，推理业务接口端口使用[`user_config.json`](../configuration/config_reference.md#motor_coordinator_config)配置文件中`coordinator_api_infer_port`定义的端口。
  - 当配置文件中无此配置项时，使用默认端口`1025`。

## 管理接口

MindIE Motor提供下列管理接口：

- [启动探针接口](./management_interfaces.md#启动探针接口)：`/startup`
- [存活探针接口](./management_interfaces.md#存活探针接口)：`/liveness`
- [就绪探针接口](./management_interfaces.md#就绪探针接口)：`/readiness`
- [实例刷新接口](./management_interfaces.md#实例刷新接口)：`/instances/refresh`
- [精度告警状态清理接口](./management_interfaces.md#精度告警状态清理接口)：`/precision/alarm_cleared`
- [根路径服务信息接口](./management_interfaces.md#根路径服务信息接口)：`/`
- [健康状态查询接口](./management_interfaces.md#健康状态查询接口)：`/health`

>[!NOTE]说明
>
> - 管理接口仅限Kubernetes集群内使用，不提供给集群外使用。
> - `/health` 与 `/metrics` 同挂 Coordinator Observability 端口（`coordinator_obs_port`，默认 `1027`），**不在**管理接口端口（`coordinator_api_mgmt_port`，默认 `1026`）上，详见[管理接口](./management_interfaces.md#健康状态查询接口)。

### 管理接口的IP/端口与配置

**管理接口IP**

- 在Kubernetes集群内，管理接口IP，使用`Coordinator`服务的IP。
  - 取值来自于[`user_config.json`](../configuration/config_reference.md#motor_coordinator_config)配置文件中的`coordinator_api_host`配置项。
  - 当配置文件中无此配置项时，则使用`Coordinator`服务部署的环境变量`POD_IP`。
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**管理接口端口**

- 在Kubernetes集群内，管理接口端口使用[`user_config.json`](../configuration/config_reference.md#motor_coordinator_config)配置文件中`coordinator_api_mgmt_port`定义的端口。
  - 当配置文件中无此配置项时，使用默认接口端口`1026`。

## 指标接口

MindIE Motor 的监控指标由 Coordinator 统一汇聚、聚合，通过 Coordinator Observability 端口对外提供。指标接口是**获取指标的唯一推荐途径**：

- [指标查询接口](./metrics_interfaces.md#接口格式)：`/metrics`，支持聚合视图（`full`/`instance`/`role`/`dp`/`node`）与返回格式（Prometheus / OpenTelemetry）

>[!NOTE]说明
>
> - 指标由 Coordinator 按周期（默认 `3` 秒）从各 Engine 采集并语义化聚合，详情参见[指标接口](./metrics_interfaces.md)。
> - Controller 侧的 `/observability/metrics` 为已弃用的转发接口，**新接入请直接使用本端口**，详见[观测接口](#观测接口)。

### 指标接口的IP/端口与配置

**指标接口IP**

- 使用Kubernetes部署时，指标接口IP使用主机IP或者域名。
- 在Kubernetes集群内，指标接口IP，使用`Coordinator`服务的IP。
  - 取值来自于[`user_config.json`](../configuration/config_reference.md#motor_coordinator_config)配置文件中的`coordinator_api_host`配置项。
  - 当配置文件中无此配置项时，则使用`Coordinator`服务部署的环境变量`POD_IP`。
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**指标接口端口**

- 使用Kubernetes部署时，指标接口端口使用`yaml`文件中`mindie-motor-coordinator-obs`元数据定义的`nodePort`，默认值为`31017`。
  - 当使用CRD模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)；
  - 当使用Multi模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/coordinator_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/coordinator_template.yaml)。
- 在Kubernetes集群内，指标接口端口使用[`user_config.json`](../configuration/config_reference.md#motor_coordinator_config)配置文件中`coordinator_obs_port`定义的端口。
  - 当配置文件中无此配置项时，使用默认端口`1027`。
- 指标接口的TLS由`mgmt_tls_config.enable_tls`控制。

>[!NOTE]说明
>指标接口端口（`coordinator_obs_port`，默认`1027`）与Controller观测接口端口（`observability_api_port`，默认`1027`）默认值相同，但属于不同组件、不同服务：指标接口位于`Coordinator`（nodePort `31017`），观测接口位于`Controller`（nodePort `31027`），使用时注意区分。

## 观测接口

Controller 观测接口提供模型服务清单与告警等运维观测数据（**不含指标**，指标请使用[指标接口](#指标接口)）：

- [模型服务清单查询接口](./observability_interface.md#模型服务清单查询接口)：`/observability/inventory`
- [告警查询接口](./observability_interface.md#告警查询接口)：`/observability/alarms`
- [对接 CCAE 前端平台](./observability_interface.md#对接-ccae-前端平台)

>[!NOTE]说明
>
> - `/observability/metrics` 已弃用：该接口是转发到 Coordinator `/metrics` 的代理，仅作兼容保留，新接入请直接使用[指标接口](#指标接口)。
> - 观测接口默认关闭，需配置 `observability_config.observability_enable=true` 后生效；主备模式下仅主 Controller 对外提供查询能力。

### 观测接口的IP/端口与配置

**观测接口IP**

- 使用Kubernetes部署时，观测接口IP使用主机IP或者域名。
- 在Kubernetes集群内，观测接口IP，使用`Controller`服务的IP。
  - 取值来自于[`user_config.json`](../configuration/config_reference.md#motor_controller_config)配置文件中的`controller_api_host`配置项。
  - 当配置文件中无此配置项时，则使用`Controller`服务部署的环境变量`POD_IP`。
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**观测接口端口**

- 使用Kubernetes部署时，观测接口端口使用`yaml`文件中`mindie-motor-observability`元数据定义的`nodePort`，默认值为`31027`。
  - 当使用CRD模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)；
  - 当使用Multi模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/controller_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/controller_template.yaml)。
- 在Kubernetes集群内，观测接口端口使用[`user_config.json`](../configuration/config_reference.md#motor_controller_config)配置文件中`observability_api_port`定义的端口。
  - 当配置文件中无此配置项时，使用默认端口`1027`。

## 安全、认证与限流

- 安全协议：`infer_tls_config.enable_tls` / `mgmt_tls_config.enable_tls` 为 `true` 时，推理/管理接口端口使用 `https`
- 请求头：
  - 必选：`Content-Type: application/json`
  - 可选：API Key
    - 对 `/v1/completions`、`/v1/chat/completions`、`/v1/messages`、`/v1/messages/count_tokens` 生效
    - Header 名称：`api_key_config.header_name`（默认 `Authorization`）
    - 前缀：`api_key_config.key_prefix`（默认 `Bearer`）
- 限流（可选）：`rate_limit_config.enable_rate_limit=true` 时启用，超限返回 `429`
