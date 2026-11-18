# 使用YAML文件配置MindIE Motor服务

## 同一套集群部署多个Motor服务

如果同一套k8s集群部署了多套PD实例，对应地会有多套Coordinator和Controller实例，那么需要为不同的Coordinator实例配置不同的端口，以避免端口冲突。

Coordinator实例的默认端口为31015，如现场部署了两套PD实例，那对应两套Coordinator实例的端口分别以31015和31016为例，修改端口的步骤如下：

  1. 进入yaml指定文件夹并打开对应文件。

      ```bash
      cd examples/deployer/yaml_template/
      vim infer_service_template.yaml
      ```

  2. 搜索`name: coordinator`关键字，能够检索到以下配置块，修改nodePort字段并保存。

      ```yaml
      ...
      - name: coordinator
        replicas: 1
        services:
        - name: mindie-motor-coordinator-infer
          spec:
            ports:
            - nodePort: 31015          # 该字段需要修改，代表业务推理面端口。
              port: 1025
              protocol: TCP
              targetPort: 1025
            selector:
              app: mindie-motor-coordinator
            sessionAffinity: None
            type: NodePort
            ...
        - name: mindie-motor-coordinator-obs
          spec:
            ports:
            - nodePort: 31017      # 该字段需要修改，代表业务指标观测端口。
              port: 1027
              protocol: TCP
              targetPort: 1027
            selector:
              app: mindie-motor-coordinator
            sessionAffinity: None
            type: NodePort
      ```

  3. 搜索`name: controller`关键字，能够检索到以下配置块，修改nodePort字段并保存。

      ```yaml
      ...
      - name: controller
        replicas: 1
        services:
        - name: mindie-motor-service
          spec:
            ports:
            - port: 1026
              protocol: TCP
              targetPort: 1026
            selector:
              app: mindie-motor-controller
            sessionAffinity: None
            type: ClusterIP
        - name: mindie-motor-observability
          spec:
            ports:
            - nodePort: 31067         # 该字段需要修改，代表服务管理面端口
              port: 1027
              protocol: TCP
              targetPort: 1027
            selector:
              app: mindie-motor-controller
            sessionAffinity: None
            type: NodePort
            ...
      ```

      >[!NOTE] 说明
      > 若`user_config.json`中配置了`"deploy_mode": "multi_deployment"`，则使用传统多YAML方式部署，需修改的内容上文相同，但需要修改的文件不同：
      >
      > - Coordinator端口：修改`coordinator_template.yaml`中的`nodePort`字段。
      > - Controller端口：修改`controller_template.yaml`中的`nodePort`字段。
      >
      > 示例命令如下
      >
      > ```bash
      > cd examples/deployer/yaml_template/
      > vim coordinator_template.yaml
      > vim controller_template.yaml
      > ```

## 将coordinator/controller/推理Pod部署在固定的服务器

默认情况下，Coordinator、Controller 和推理 Pod 会在 K8s 集群的服务器之间随机调度。如需将各 Pod 固定到指定服务器，直接在 `user_config.json` 的 `motor_deploy_config` 中配置 `nodeSelector` 即可，无需修改部署 YAML。

| 字段 | 作用 |
|------|------|
| `coordinator_node_selector` | 固定 Coordinator Pod 所在节点 |
| `controller_node_selector` | 固定 Controller Pod 所在节点 |
| `prefill_node_selector` | 固定 Prefill 实例 Pod 所在节点 |
| `decode_node_selector` | 固定 Decode 实例 Pod 所在节点 |

字段值为 JSON 对象，key 为节点标签名，value 为标签值，可填写节点上的任意标签，多个键值对需同时满足（AND 关系）。`kubernetes.io/hostname` 是 K8s 内置的节点名标签，可直接使用；使用自定义标签时，先执行 `kubectl label node <节点名> <key>=<value>` 为节点打标签。

以下示例将 P 实例部署在 `work-19`，将 D 实例、Controller 和 Coordinator 部署在 `work-11`（仅列出与本功能相关的字段，其余字段保持原配置不变）：

```json
{
  "motor_deploy_config": {
    "prefill_node_selector": {
      "kubernetes.io/hostname": "work-19"
    },
    "decode_node_selector": {
      "kubernetes.io/hostname": "work-11"
    },
    "controller_node_selector": {
      "kubernetes.io/hostname": "work-11"
    },
    "coordinator_node_selector": {
      "kubernetes.io/hostname": "work-11"
    }
  }
}
```

修改配置后重新部署并查看调度结果：

```bash
cd examples/deployer
python deploy.py --config_dir <配置目录>
kubectl get pod -n <命名空间> -o wide
```

>[!NOTE] 说明
>
> - 自定义 selector 会与 deployer 根据 `hardware_type` 生成的硬件标签（如 `accelerator`、`accelerator-type`）合并，同名 key 以 `user_config.json` 为准。
> - 上述字段对 `infer_service_set` 和 `multi_deployment` 两种部署模式均生效；`single_container` 模式所有组件共用一个 Pod，不按组件生效。
> - PD混部场景的 union 推理实例不支持 `prefill_node_selector` / `decode_node_selector`（Controller、Coordinator 的 selector 仍生效）；如需固定 union 实例节点，仍需在 `infer_service_template.yaml`（`infer_service_set`）或 `engine_template.yaml`（`multi_deployment`）的 union 角色 `spec.template.spec.nodeSelector` 中手工配置。
