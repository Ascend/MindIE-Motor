# 接口说明

MindIE Motor提供推理[业务接口](#业务接口)、[管理接口](#管理接口)、[监控接口](#监控接口)、[观测接口](#观测接口)和[内部接口](#内部接口)。

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
  - 取值来自于`user_config.json`配置文件中的`coordinator_api_host`配置项。
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。
  - 当配置文件中无此配置项时，则使用`Coordinator`服务部署的环境变量`POD_IP`。
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**推理业务接口端口**

- 使用Kubernetes部署时，推理业务接口端口使用`yaml`文件中`mindie-motor-coordinator-infer`元数据定义的`nodePort`，默认值为`31015`。
  - 当使用CRD模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)；
  - 当使用Multi模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/coordinator_template.yaml`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/deployer/yaml_template/coordinator_template.yaml)。
- 在Kubernetes集群内，推理业务接口端口使用`user_config.json`配置文件中`coordinator_api_infer_port`定义的端口。
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。
  - 当配置文件中无此配置项时，使用默认端口`1025`。

## 管理接口

MindIE Motor提供下列管理接口：

- [启动探针接口](./management_interfaces.md#启动探针接口)：`/startup`
- [存活探针接口](./management_interfaces.md#存活探针接口)：`/liveness`
- [就绪探针接口](./management_interfaces.md#就绪探针接口)：`/readiness`
- [实例刷新接口](./management_interfaces.md#实例刷新接口)：`/instances/refresh`
- [根路径服务信息接口](./management_interfaces.md#根路径服务信息接口)：`/`

>[!NOTE]说明
>
> 管理接口仅限Kubernetes集群内使用，不提供给集群外使用。

### 管理接口的IP/端口与配置

**管理接口IP**

- 在Kubernetes集群内，管理接口IP，使用`Coordinator`服务的IP。
  - 取值来自于`user_config.json`配置文件中的`coordinator_api_host`配置项。 
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。 
  - 当配置文件中无此配置项时，则使用`Coordinator`服务部署的环境变量`POD_IP`。 
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**管理接口端口**

- 在Kubernetes集群内，管理接口端口使用`user_config.json`配置文件中`coordinator_api_mgmt_port`定义的端口。
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。
  - 当配置文件中无此配置项时，使用默认接口端口`1026`。

## 监控接口

MindIE Motor提供下列监控接口：

- [健康状态查询接口](./monitoring_interfaces.md#健康状态查询接口)：`/health`
- [指标查询接口](./monitoring_interfaces.md#指标查询接口)：`/metrics`

### 监控接口的IP/端口与配置

**监控接口IP**

- 使用Kubernetes部署时，监控接口IP使用主机IP或者域名。
- 在Kubernetes集群内，监控接口IP，使用`Coordinator`服务的IP。
  - 取值来自于`user_config.json`配置文件中的`coordinator_api_host`配置项。 
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。 
  - 当配置文件中无此配置项时，则使用`Coordinator`服务部署的环境变量`POD_IP`。 
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**监控接口端口**

- 使用Kubernetes部署时，监控接口端口使用`yaml`文件中`mindie-motor-coordinator-obs`元数据定义的`nodePort`，默认值为`31017`。 
  - 当使用CRD模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)； 
  - 当使用Multi模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/coordinator_template.yaml`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/deployer/yaml_template/coordinator_template.yaml)。
- 在Kubernetes集群内，监控接口端口使用`user_config.json`配置文件中`coordinator_obs_port`定义的端口。
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。 
  - 当配置文件中无此配置项时，使用默认端口`1027`。

## 观测接口

MindIE Motor提供下列观测接口：

- [模型服务清单查询接口](./observability_interface.md#模型服务清单查询接口)：`/observability/inventory`
- [监控指标查询接口](./observability_interface.md#监控指标查询接口)：`/observability/metrics`
- [告警查询接口](./observability_interface.md#告警查询接口)：`/observability/alarms`
- [对接 CCAE 前端平台](./observability_interface.md#对接-ccae-前端平台)

### 观测接口的IP/端口与配置

**观测接口IP**

- 使用Kubernetes部署时，观测接口IP使用主机IP或者域名。
- 在Kubernetes集群内，观测接口IP，使用`Controller`服务的IP。
  - 取值来自于`user_config.json`配置文件中的`controller_api_host`配置项。 
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。 
  - 当配置文件中无此配置项时，则使用`Controller`服务部署的环境变量`POD_IP`。 
  - 当环境变量`POD_IP`也不存在或为空时，使用默认值`127.0.0.1`。

**观测接口端口**

- 使用Kubernetes部署时，观测接口端口使用`yaml`文件中`mindie-motor-observability`元数据定义的`nodePort`，默认值为`31027`。
  - 当使用CRD模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)； 
  - 当使用Multi模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/controller_template.yaml`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/deployer/yaml_template/controller_template.yaml)。
- 在Kubernetes集群内，观测接口端口使用`user_config.json`配置文件中`observability_api_port`定义的端口。
  - 配置文件参考[`examples/features/config_sample.json`](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/config_sample.json)。
  - 当配置文件中无此配置项时，使用默认端口`1027`。

## 内部接口

EngineServer提供下列内部接口：

- [Engine Server 快照接口](./engine_server_interfaces.md#engine-server-快照接口)，包括：
  - [设备侧快照保存接口](./engine_server_interfaces.md#设备侧快照保存接口)：`/suspend`
  - [设备解锁接口](./engine_server_interfaces.md#设备解锁接口)：`/device_unlock`
  - [设备侧快照恢复接口](./engine_server_interfaces.md#设备侧快照恢复接口)：`/resume`
- [MetaServer转发接口](./engine_server_interfaces.md#metaserver转发接口)：`/v1/metaserver`

>[!NOTE]说明
>
> Engine Server 内部接口挂载在 Engine Server 推理面，**不在** Coordinator 推理接口上提供服务。

### 内部接口的IP/端口

- 内部接口IP：Engine Server 所在节点的 IP 或 `engine_server --host` 绑定的地址。
- 内部接口端口：`engine_server --port` 指定的端口。

## 安全、认证与限流

- 安全协议：`infer_tls_config.tls_enable` / `mgmt_tls_config.tls_enable` 为 `true` 时，推理/管理接口端口使用 `https`
- 请求头：
  - 必选：`Content-Type: application/json`
  - 可选：API Key
    - 对 `/v1/completions`、`/v1/chat/completions`、`/v1/messages`、`/v1/messages/count_tokens` 生效
    - Header 名称：`api_key_config.header_name`（默认 `Authorization`）
    - 前缀：`api_key_config.key_prefix`（默认 `Bearer`）
- 限流（可选）：`rate_limit_config.enable_rate_limit=true` 时启用，超限返回 `429`
