# 常用维护技巧

---

## 如何在同一套集群部署多个Motor服务（如何修改业务推理端口）？

如果同一套k8s集群部署了多套PD实例，对应的会有多套Coordinator和Controller实例，那么需要为不同的Coordinator实例配置不同的端口，以避免端口冲突。

Coordinator实例的默认端口为31015，如现场部署了两套PD实例，那对应两套Coordinator实例的端口分别以31015和31016为例，修改端口的步骤如下：

  1. 进入yaml指定文件夹并打开对应文件：

      ```yaml
      cd examples/deployer/yaml_template/
      vim infer_service_template.yaml
      ```

  2. 搜索name: coordinator关键字，能够检索到以下配置块，修改nodePort字段并保存：

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

  3. 搜索name: controller关键字，能够检索到以下配置块，修改nodePort字段并保存：

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

  >[!NOTE]说明
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

---

## 如何将coordinator/controller/推理pod部署在固定的服务器？

默认情况下，Coordinator、Controller和推理pod将在k8s集群中的服务器之间随机分配，如果希望这些pod部署在固定服务器，可参考以下操作。

### PD分离场景

  1. 在k8s的master节点，执行以下命令，为服务器中的各个服务器打标签。

      ```yaml
      kubectl label node {node_name} key=value
      ```

      - `node_name`：填写服务器名称，可通过 kubectl get node命令查询。
      - `key`：标签名，自定义填写。
      - `value`：标签值，自定义填写。

      例如：

      ```bash
      # controller部署在node-33-137服务器上
      kubectl label node node-33-137 mindie_controller=controller
      # coordinator部署在node-33-138服务器上
      kubectl label node node-33-138 mindie_coordinator=coordinator
      # PD分离场景下，P实例部署在node-33-201服务器上
      kubectl label node node-33-201 motor_role=prefill
      # PD分离场景下，D实例部署在node-33-203服务器上
      kubectl label node node-33-203 motor_role=decode
      ```

  2. 修改Controller实例的初始化文件。

     执行`vim infer_service_template.yaml`命令，搜索`name: controller`，在如下所示的配置块中新增两个字段（`mindie_controller`和`controller`分别是第一步中创建的标签名和标签值）。

      ```yaml
      ...
      - name: controller
        replicas: 1
        ...
        spec:
          replicas: 1
          selector:
            matchLabels:
              app: mindie-motor-controller
          template:
            metadata:
              labels:
                app: mindie-motor-controller
                deploy-name: mindie-motor-controller
            spec:
              nodeSelector:                    # 新增
                mindie_controller: controller  # 新增
              serviceAccountName: mindie-motor-controller
              terminationGracePeriodSeconds: 0
              securityContext:
                fsGroup: 1001
      ...
      ```

  3. 修改Coordinator实例的初始化文件。

     执行`vim infer_service_template.yaml`命令，搜索`name: coordinator`，在如下所示的配置块中新增两个字段（`mindie_coordinator`和`coordinator`分别是第一步中创建的标签名和标签值）。

      ```yaml
      ...
      - name: coordinator
        replicas: 1
        ...
        spec:
          replicas: 1
          selector:
            matchLabels:
              app: mindie-motor-coordinator
          template:
            metadata:
              labels:
                app: mindie-motor-coordinator
            spec:
              nodeSelector:                      # 新增
                mindie_coordinator: coordinator  # 新增
              terminationGracePeriodSeconds: 0
              automountServiceAccountToken: false
              securityContext:
                fsGroup: 1001
      ...
      ```

  4. 修改推理Pod的初始化文件。

     执行`vim infer_service_template.yaml`命令，分别搜索`name: prefill`和`name: decode`，在各自的`template.spec.nodeSelector`中追加角色标签（`motor_role`的值与是第一步中创建的标签名和标签值保持一致）：

      ```yaml
      ...
      - name: prefill
        ...
          template:
            spec:
              schedulerName: volcano
              nodeSelector:
                accelerator: huawei-Ascend910
                accelerator-type: module-910b-8
                motor_role: prefill          # 新增
      ...
      - name: decode
        ...
          template:
            spec:
              schedulerName: volcano
              nodeSelector:
                accelerator: huawei-Ascend910
                accelerator-type: module-910b-8
                motor_role: decode           # 新增
      ...
      ```

  5. 修改完成后重新部署并验证调度结果。

      ```bash
      cd examples/deployer
      python deploy.py --config_dir <配置目录>
      kubectl get pod -n <命名空间> -o wide
      ```

      可以观察各pod将按照标签关系被调度至不同节点。

  >[!NOTE]说明
  > 若`user_config.json`中配置了`"deploy_mode": "multi_deployment"`，打标签的方式与上文相同，但需修改的模板文件不同：
  >
  > - Controller：修改`controller_template.yaml`，在`template.spec`下新增`nodeSelector`。
  > - Coordinator：修改`coordinator_template.yaml`，在`template.spec`下新增`nodeSelector`。
  > - 推理Pod（PD分离）：修改`engine_template.yaml`，在`template.spec.nodeSelector`中追加`motor_role: prefill`或`motor_role: decode`。
  >
  > 示例命令如下
  >
  > ```bash
  > cd examples/deployer/yaml_template/
  > vim controller_template.yaml
  > vim coordinator_template.yaml
  > vim engine_template.yaml
  > ```

### PD混部场景

  1. 在k8s的master节点，执行以下命令，为服务器中的各个服务器打标签。

      ```bash
      kubectl label node {node_name} key=value
      ```

      - `node_name`：填写服务器名称，可通过 kubectl get node命令查询。
      - `key`：标签名，自定义填写。
      - `value`：标签值，自定义填写。

      例如：

      ```bash
      # controller部署在node-33-137服务器上
      kubectl label node node-33-137 mindie_controller=controller
      # coordinator部署在node-33-138服务器上
      kubectl label node node-33-138 mindie_coordinator=coordinator
      # PD混部场景下，混部推理实例部署在node-33-201服务器上
      kubectl label node node-33-201 motor_role=hybrid
      ```

  2. 修改Controller实例的初始化文件。

     执行`vim controller_template.yaml`命令，搜索`name: mindie-motor-controller`，在如下所示的配置块中新增两个字段（`mindie_controller`和`controller`分别是第一步中创建的标签名和标签值）。

      ```yaml
      ...
      template:
        metadata:
          labels:
            app: mindie-motor-controller
            deploy-name: mindie-motor-controller
        spec:
          nodeSelector:                    # 新增
            mindie_controller: controller  # 新增
          serviceAccountName: mindie-motor-controller
          terminationGracePeriodSeconds: 0
          securityContext:
            fsGroup: 1001
      ...
      ```

  3. 修改Coordinator实例的初始化文件。

     执行`vim coordinator_template.yaml`命令，搜索`name: mindie-motor-coordinator`，在如下所示的配置块中新增两个字段（`mindie_coordinator`和`coordinator`分别是第一步中创建的标签名和标签值）。

      ```yaml
      ...
      template:
        metadata:
          labels:
            app: mindie-motor-coordinator
        spec:
          nodeSelector:                      # 新增
            mindie_coordinator: coordinator  # 新增
          terminationGracePeriodSeconds: 0
          automountServiceAccountToken: false
          securityContext:
            fsGroup: 1001
      ...
      ```

  4. 修改推理Pod的初始化文件。

     执行`vim engine_template.yaml`命令，在`template.spec.nodeSelector`中追加角色标签（`motor_role`的值与第一步中创建的标签名和标签值保持一致）：

      ```yaml
      ...
      template:
        spec:
          schedulerName: volcano
          nodeSelector:
            accelerator: huawei-Ascend910
            accelerator-type: module-910b-8
            motor_role: hybrid          # 新增
      ...
      ```

  5. 修改完成后重新部署并验证调度结果。

      ```bash
      cd examples/deployer
      python deploy.py --config_dir <配置目录>
      kubectl get pod -n <命名空间> -o wide
      ```

      可以观察各pod将按照标签关系被调度至不同节点。

  >[!NOTE]说明
  > PD混部场景默认通过`multi_deployment`方式部署（`user_config.json`中需包含`hybrid_instances_num`等混部字段），无需修改`infer_service_template.yaml`。
