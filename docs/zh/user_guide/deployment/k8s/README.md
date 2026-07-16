# 部署方式配置说明

MindIE Motor支持两种业务拓扑结构（**影响功能**）和 三种服务部署方式（**不影响功能，无需特别关注**），本文档将对相关内容进行说明，避免用户混淆。

简单说明

| 分类 | 选项 | 说明 |
|------|------|--------------|
| 业务拓扑 | PD 分离、PD 混部 | 决定 Prefill / Decode 是否拆分部署，**影响推理性能** |
| 服务部署方式 | `infer_service_set`、`multi_deployment`、`single_container` | 仅影响服务资源如何创建，部署方式**对服务功能无影响，本文仅介绍工作原理，一般情况下无需关注该字段**。未显示配置时使用`infer_service_set`，。

---

## 业务拓扑

业务拓扑决定服务功能与运行形态，需按场景选择。

### PD 分离

Prefill 与 Decode 分属不同实例，适用于需要独立规划 P/D 资源、追求更高吞吐的场景。详细步骤见 [PD 分离服务部署](./pd_disaggregation_deployment.md)。

### PD 混部

Prefill 与 Decode 由同一类实例（union）承载，适用于快速验证与中小规模部署。详细步骤见 [PD 混部服务部署](./pd_aggregation_deployment.md)。

---

## 部署方式

服务部署方式仅**影响服务资源通过哪种方式创建**，可以通过修改user_config.json文件指定，如下。未配置时默认为 `infer_service_set`。

```json
{
  "motor_deploy_config": {
    "deploy_mode": "multi_deployment",  // 代表通过multi_deployment方式部署服务。
    ...
  }
}
```

配置选项说明如下

| 取值 | 说明 |
|------|------|
| `infer_service_set` | 默认方式。生成单个 `infer_service.yaml`，由 CRD controller 统一拉起 controller、coordinator、prefill、decode（PD 分离）或 union（PD 混部）等 pod。|
| `multi_deployment` | 生成 controller、coordinator、engine、kv_pool 等多个独立 YAML，分别创建pod。|
| `single_container` | 单容器方式。将 P/D 合并到单个容器中运行，适用于小规模或测试场景。 |

如需了解user_config.json文件的其余配置字段，请参考[全量配置说明](../../configuration/config_reference.md)。
