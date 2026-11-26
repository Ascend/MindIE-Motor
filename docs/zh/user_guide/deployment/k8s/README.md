# 业务拓扑与服务部署方式

MindIE Motor支持两种业务拓扑结构和四种服务部署方式，本文档将对相关内容进行说明，避免用户混淆。

**表 1**  业务拓扑与服务部署方式说明

| 分类 | 选项 | 说明 |
|------|------|--------------|
| 业务拓扑 | <ul><li>PD分离</li><li>PD混部</li></ul> | 决定Prefill/Decode是否拆分部署，对推理的性能有一定的影响。 |
| 服务部署方式 | <ul><li>infer_service_set</li><li>rbg</li><li>multi_deployment</li><li>single_container</li></ul> | 影响服务资源的创建、生命周期与扩缩容控制面。未显式配置时默认使用infer_service_set部署方式。 |

## 业务拓扑

业务拓扑决定服务功能与运行形态，需根据具体场景进行选择。

**PD分离**

Prefill与Decode分属不同实例，适用于需要独立规划P/D资源、追求更高吞吐的场景。

- vLLM推理引擎：详情请参考[PD分离服务部署（基于vLLM推理引擎）](../README.md#k8s-pd-disaggregation)。
- SGLang推理引擎：详情请参考[PD分离服务部署（基于SGLang推理引擎）](../README.md#k8s-pd-disaggregation-sglang)。

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
| rbg | 生成RoleBasedGroup、业务Service及配套RBAC，由RBG控制器创建RoleInstance和Pod。支持RBG手动扩缩容及通过RoleBasedGroupScalingAdapter对接HPA。按业务拓扑和推理引擎参见[PD混部](./pd_aggregation_deployment.md#使用rbg方式部署)、[vLLM PD分离](./pd_disaggregation_deployment.md#使用rbg方式部署)或[SGLang PD分离](./pd_disaggregation_sglang.md#使用rbg方式部署)。|
| multi_deployment | 生成controller、coordinator、engine、kv_pool等多个独立的YAML文件，分别创建Pod。|
| single_container | 单容器方式。将P/D合并到单个容器中运行，适用于小规模或测试场景。 |

如需了解user_config.json文件的其余配置字段，请参考[user_config.json配置文件全量参数说明](../../configuration/config_reference.md)。

<a id="deployment-security-precheck"></a>

## 部署前安全预检

Kubernetes 命令行部署时，`deploy.py` 会在启动服务、`--dry-run`、`--update_config` 和 `--update_instance_num` 前检查 `user_config.json`。如果看到 `Deployment pre-check requires attention.`，请检查以下配置。

- 将 `motor_deploy_config.tls_config.infer_tls_config.enable_tls` 设为 `true`，并按[配置参数说明](../../configuration/config_reference.md)配置推理 TLS 证书。
- 将 `motor_coordinator_config.mgmt_api_key_config.enable_api_key` 设为 `true`，并在 `api_key_file` 中指定符合[管理接口认证要求](../../api/management_interfaces.md)的密钥文件路径。

生产环境请完成安全配置后重新执行原命令。仅在隔离测试环境需要沿用现有配置时，可在原命令末尾追加 `--skip-check`。例如，在 `examples/deployer` 目录启动服务时可执行以下命令，将 `/path/to/config` 替换为实际配置目录。

```bash
python3 deploy.py --config_dir /path/to/config --skip-check
```

`--skip-check` 仅跳过部署前的安全配置检查。运行时沿用 `user_config.json` 中的安全配置。推理 TLS 关闭时，推理入口使用明文 HTTP。管理 API Key 认证关闭时，管理接口跳过该 Key 的校验。

<a id="verify-inference-tls"></a>

## 通过 HTTPS 验证推理服务

推理 TLS 启用并成功加载证书后，推理入口使用 HTTPS。请用服务证书覆盖的域名或 IP 访问 Coordinator 推理端口，并在客户端准备签发该服务证书的 CA 证书（PEM 格式）。以下示例使用默认 NodePort `31015`，请按实际地址和端口修改 `INFER_URL`，并将 `INFER_CA` 改为客户端可读取的 CA 证书路径。

```bash
INFER_URL="https://infer.example.com:31015"
INFER_CA="/path/to/infer-ca.pem"
curl --cacert "$INFER_CA" "$INFER_URL/v1/models"
```

验证 `/v1/chat/completions` 等推理请求时，同样使用 HTTPS 地址并添加 `--cacert "$INFER_CA"`，请求体沿用相应接口示例。启用了推理 API Key 认证时，请按[接口鉴权说明](../../api/README.md)携带请求头。
