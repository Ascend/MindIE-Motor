# 业务拓扑与服务部署方式

MindIE Motor支持两种业务拓扑结构和三种服务部署方式，本文档将对相关内容进行说明，避免用户混淆。

简单说明

| 分类 | 选项 | 说明 |
|------|------|--------------|
| 业务拓扑 | <ul><li>PD分离</li><li>PD混部</li></ul> | 决定Prefill/Decode是否拆分部署，对推理的性能有一定的影响。 |
| 服务部署方式 | <ul><li>infer_service_set</li><li>multi_deployment</li><li>single_container</li></ul> | 仅影响服务资源如何创建，部署方式对服务功能无影响，本文仅介绍工作原理，一般情况下无需关注该字段。未显式配置时默认使用infer_service_set部署方式。 |

## 业务拓扑

业务拓扑决定服务功能与运行形态，需根据具体场景进行选择。

**PD分离**

Prefill与Decode分属不同实例，适用于需要独立规划P/D资源、追求更高吞吐的场景。

- vLLM推理引擎：详情请参考[PD分离服务部署（基于vLLM推理引擎）](./pd_disaggregation_deployment.md)。
- SGLang推理引擎：详情请参考[PD分离服务部署（基于SGLang推理引擎）](./pd_disaggregation_sglang.md)。

**PD混部**

Prefill与Decode由同一类实例（union）承载，适用于快速验证与中小规模部署。详情请参考[PD混部服务部署](./pd_aggregation_deployment.md)。

## 部署方式

服务部署方式仅影响服务资源通过哪种方式创建，可以通过修改user_config.json文件指定，未配置时默认为`infer_service_set`方式，配置示例如下。

```json
{
  "motor_deploy_config": {
    "deploy_mode": "multi_deployment",  // 代表通过multi_deployment方式部署服务。
    ...
  }
}
```

**表 2** 配置选项参数解释

| 取值 | 说明 |
|------|------|
| infer_service_set | 默认方式。生成单个infer_service.yaml文件，由CRD controller统一拉起controller、coordinator、prefill、decode（PD分离）或 union（PD混部）等Pod。|
| multi_deployment | 生成controller、coordinator、engine、kv_pool等多个独立的YAML文件，分别创建Pod。|
| single_container | 单容器方式。将P/D合并到单个容器中运行，适用于小规模或测试场景。 |

如需了解user_config.json文件的其余配置字段，请参考[全量配置说明](../../configuration/config_reference.md)。
