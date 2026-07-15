# 容器快照

## 特性介绍

容器快照特性用于保存实例节点容器的运行状态，并在实例重调度等场景中快速恢复推理服务。Motor 服务框架负责 Device 侧的 suspend、resume 及恢复后的控制面注册；MindCluster 或用户负责对实例节点容器执行 Host 侧 checkpoint。

容器快照由以下两部分组成：

- 落盘至宿主机挂载路径的运行时模型权重。
- 容器 Host 快照镜像，其中包含 Device 快照状态。

该特性支持两类应用场景：

- **快照默认应用场景**：MindCluster 实例重调度。MindCluster 挂载快照元数据、查询稳态点、执行容器 checkpoint 并保存 Host 快照镜像。
- **用户自定义应用场景**：用户自行创建并挂载快照元数据文件、查询稳态点、执行容器 checkpoint，并管理 Host 快照镜像与运行时权重。

## 环境约束

- **操作系统**：仅支持 EulerOS R15C10 / HCE 3.0，且需要预装 CRIU 3.19 与 grus。
- **容器运行时**：仅支持 containerd。
- **推理引擎**：必须支持 Device 快照的保存与恢复能力，并提供对应的 suspend 和 resume 接口。

## 容器快照制作

容器快照制作流程如下：

1. 实例冷启动并进入健康状态后，Engine Server 自动执行 suspend，锁定 Device 状态、保存 Device 快照，并将运行时模型权重写入 `model_save_path`。
2. 当本节点全部 Engine Server 均完成 suspend 后，实例节点容器到达稳态点。
   - MindCluster 实例重调度场景：通过 Node Manager 的 `/readiness` 返回 `200` 判断。
   - 用户自定义应用场景：通过 `/node-manager/status` 返回 `200 {"status": true}` 判断。
3. 到达稳态点后，MindCluster 或用户使用 grus 对实例节点容器执行 checkpoint，保存容器 Host 快照镜像。
4. Host 侧 checkpoint 完成后，将元数据字段 `checkpoint` 更新为 `"done"`。Engine Server 检测到该状态后解锁 Device，冷启动实例恢复提供推理服务。

处于 checkpoint 过程中的实例无法提供推理服务。到达稳态点但 `checkpoint` 尚未完成时，Node Manager 暂停向 Controller 上报正常心跳。

## 容器快照恢复

容器快照恢复流程如下：

1. MindCluster 或用户从 Host 快照镜像恢复实例节点容器，并挂载对应的运行时权重和快照元数据文件。
2. Node Manager 从元数据读取 `job_name` 和 `namespace`，刷新 Pod IP 与 Controller DNS，然后向 Controller 重新注册。
3. Controller 下发启动命令。Node Manager 更新快照元数据文件中的 `model_load_path` 与 `data_parallel_master_ip` 字段；快照恢复场景不会重新创建 Engine Server 进程。
4. Engine Server 使用元数据中的 `model_load_path` 与 `data_parallel_master_ip` 执行 resume。全部 endpoint 恢复为 `NORMAL` 后，实例重新进入就绪状态。

## 启用制作容器快照配置

### user_config.json

在 `user_config.json` 中增加容器快照相关配置组 `motor_container_snapshot_config`：

```json
"motor_container_snapshot_config": {
    "enable_snapshot": true,
    "snapshot_metadata_path": "/path/to/snapshot_metadata.json"
}
```

**enable_snapshot**：容器快照总开关，缺省为 `false`。该字段为 `false` 时，其余字段均不生效；为 `true` 时，表示启用实例节点容器的快照制作与恢复能力。

**snapshot_metadata_path**：快照元数据文件在容器内的路径，缺省为空字符串。

- 配置为空时，进入快照默认应用场景，即 MindCluster 实例重调度。快照元数据由 MindCluster 通过 ConfigMap 挂载，Node Manager 将其复制到默认可写路径 `/snapshot/snapshot_metadata.json` 后使用。
- 配置非空时，进入用户自定义应用场景。用户必须预先创建快照元数据文件，并将其挂载至配置指定的容器路径。

快照元数据文件必须是 JSON 对象，下列字段的值均为字符串：

| 字段 | 使用阶段 | 准备要求 | 说明 |
|------|----------|----------|------|
| `model_save_path` | 快照制作 | 制作容器快照前必须准备 | 运行时模型权重的落盘路径，必须是宿主机挂载路径 |
| `model_load_path` | 快照恢复 | 从容器快照恢复前必须准备 | 运行时模型权重的加载路径，必须是宿主机挂载路径 |
| `job_name` | 快照恢复 | 从容器快照恢复前必须准备 | 推理实例的唯一标识，恢复后注册时用于更新 Node Manager 的任务名 |
| `namespace` | 快照恢复 | Controller 使用集群内 `.svc.cluster.local` DNS 时必须准备 | 推理服务所属 namespace，用于更新 Controller DNS；非集群 DNS 场景可不配置 |
| `data_parallel_master_ip` | 快照恢复 | 可不预先配置 | 实例 Master DP 所在 Pod 的 IP；优先使用文件中的值，未配置时由 Node Manager 写入 Controller 下发值 |
| `checkpoint` | 快照制作 | Host 侧 checkpoint 完成后写入 | 更新为 `"done"` 后，Engine Server 解锁 Device，冷启动实例恢复推理服务 |

用户自定义应用场景，制作容器快照前, 需要准备 `model_save_path` 字段; 从容器快照恢复前，需要准备 `model_load_path` 和 `job_name`；使用集群内 Controller DNS 时还需准备 `namespace`。

## 容器快照应用场景

通过直接加载实例节点容器快照，可缩短实例恢复至就绪状态的时间。默认应用场景为 MindCluster 实例重调度。

### 实例重调度

容器快照特性默认与 MindCluster 实例重调度配合使用。该场景下，快照元数据文件由 MindCluster 通过 ConfigMap 挂载，`snapshot_metadata_path` 可缺省或留空。

Motor 服务框架需为实例节点 Pod 配置 Kubernetes Readiness Probe，供 MindCluster 查询实例节点是否到达稳态点。MindCluster 在实例节点到达稳态点后执行 checkpoint，保存容器 Host 快照镜像。

MindCluster 侧的环境要求、组件部署和使用流程请参见[容器快照部署及使用](https://gitcode.com/Ascend/mind-cluster/blob/branch_v26.1.0/docs/zh/scheduling/04_usage/09_infer_operator_best_practice/06_container_snapshot_usage.md)。

**容器快照特性在实例重调度应用场景下的约束**：

- MindCluster 保存实例节点容器的 Host 快照镜像仅支持 CRD 部署方式。
- MindCluster 仅为同种实例保存一份容器 Host 快照镜像；例如 2P1D 场景下，仅为首个 P 实例保存容器 Host 快照镜像。
- 为便于管理实例的容器 Host 快照镜像，MindCluster 当前仅支持将 Host 快照镜像保存在集群共享存储路径下。

**Motor 服务框架侧需配置**：

在 `user_config.json` 中添加：

```json
"motor_container_snapshot_config": {
    "enable_snapshot": true
}
```

在 `infer_service_template.yaml` 中修改配置。以下以 Union 实例为例，仅展示改动点；YAML 基准配置请参见 `examples/deployer/yaml_template`：

```yaml
......
    - name: union
      replicas: 4
      workload:
        apiVersion: apps/v1
        kind: StatefulSet
      # --------TODO 1: 在metadata里添加snapshot 标签--------
      metadata:
        labels:
          infer.huawei.com/container-snapshot: 'true'
      # ----------------------------------------------------
      spec:
        # --------TODO 2: 添加pod并行启动策略--------
        podManagementPolicy: Parallel
        # ------------------------------------------
        replicas: 2
        selector:
          matchLabels:
            app: mindie-server
        template:
          metadata:
            labels:
              fault-scheduling: grace
              fault-retry-times: "10000"
              app: mindie-server
              ring-controller.atlas: ascend-910b
          spec:
            schedulerName: volcano
            nodeSelector:
              accelerator: huawei-Ascend910
              accelerator-type: module-910b-8
            terminationGracePeriodSeconds: 30
            automountServiceAccountToken: false
            securityContext:
              fsGroup: 1001
            containers:
            - image: mindie:1.0.0-aarch64-800I-A2
              imagePullPolicy: IfNotPresent
              name: mindie-server
              securityContext:
                allowPrivilegeEscalation: false
                # 由于线程创建依赖的 syscall 在不同架构上存在差异, 在seccomp的 RuntimeDefault 默认策略下会被过滤拦截
                # 因此将seccompProfile.type 设置为 Unconfined，禁用 seccomp 系统调用过滤, 以获得最佳兼容性
                # 请注意，Unconfined 会增加容器攻击面，仅建议在确有需要时使用
                # 如果您的集群在 seccompProfile.type: RuntimeDefault 下运行正常，可直接使用 RuntimeDefault，以获得运行时默认的安全过滤
                # 具体详见资料描述: MindIE Motor/examples/features/pod_permission_guide/README.md
                seccompProfile:
                  type: Unconfined
              # --------TODO 3: 启用readiness探针用于Mindcluster探测稳态点--------
              readinessProbe:
                exec:
                  command:
                  - bash
                  - -c
                  - "$CONFIGMAP_PATH/probe.sh readiness"
                periodSeconds: 5
                timeoutSeconds: 4
                failureThreshold: 12
              # -----------------------------------------------------------------
              env:
              - name: POD_IP
                valueFrom:
                  fieldRef:
                    fieldPath: status.podIP
              - name: HOST_IP
                valueFrom:
                  fieldRef:
                    fieldPath: status.hostIP
              - name: CRIU_LOG_LEVEL
                value: "3"
              - name: CONFIGMAP_PATH
                value: /mnt/configmap
              - name: CONFIG_PATH
                value: /usr/local/Ascend/pyMotor/conf
              # --------TODO 4: 添加容器host快照镜像保存路径(该路径要求是共享存储路径， 且不能在容器内挂载)--------
              - name: host_snapshot_dir_path
                value: "path/to/container_host_image"
              # ---------------------------------------------------------------------------------------------
              lifecycle:
                preStop:
                  exec:
                    command: ["bash", "-c", "$CONFIGMAP_PATH/prestop.sh"]
              command: ["/bin/bash", "-c", "source /mnt/configmap/boot.sh;"]
              resources:
                requests:
                  memory: "64Gi"
                  cpu: "16"
                  huawei.com/Ascend910: 1
                limits:
                  memory: "256Gi"
                  cpu: "64"
                  huawei.com/Ascend910: 1
              volumeMounts:
              # --------TODO 5: 取消宿主机落盘挂载--------
              # - name: data
              #   mountPath: /data
              #   readOnly: true
              # ------------------------------------------
              - name: motor-config
                mountPath: /mnt/configmap
              - name: queue-schedule
                mountPath: /var/queue_schedule
              # --------TODO 5: 取消宿主机落盘挂载--------
              # - name: dshm
              #   mountPath: /dev/shm
              # - name: coredump
              #   mountPath: /var/coredump
              # ------------------------------------------
              - name: mnt
                mountPath: /mnt
              - name: hccn-tool
                mountPath: /usr/local/Ascend/driver/tools/hccn_tool
              - name: hccn-conf
                mountPath: /etc/hccn.conf
              - name: weight-mount
                mountPath: /mnt/weight
              # --------TODO 5: 取消宿主机落盘挂载--------
              # - name: plog-path
              #   mountPath: /root/ascend/log
              # ------------------------------------------

              # --------TODO 6: 增加以下挂载路径--------
              - name: snapshot-weight
                mountPath: /snapshot/weight
              - name: dcmi
                mountPath: /usr/local/dcmi
              - name: ascend-driver
                mountPath: /usr/local/Ascend/driver
                mountPropagation: "HostToContainer"
              - name: npu-smi
                mountPath: /usr/local/bin/npu-smi
              # ---------------------------------------

            volumes:
            # --------TODO 5: 取消宿主机落盘挂载--------
            # - name: data
            #   hostPath:
            #     path: /data
            # ------------------------------------------
            - name: motor-config
              configMap:
                name: motor-config
                defaultMode: 360
            - name: queue-schedule
              hostPath:
                path: /var/queue_schedule
            # --------TODO 5: 取消宿主机落盘挂载--------
            # - name: dshm
            #   emptyDir:
            #     medium: Memory
            #     sizeLimit: 4Gi
            # - name: coredump
            #   hostPath:
            #     path: /var/coredump
            #     type: DirectoryOrCreate
            # ------------------------------------------
            - name: mnt
              hostPath:
                path: /mnt
            - name: hccn-tool
              hostPath:
                path: /usr/local/Ascend/driver/tools/hccn_tool
            - name: hccn-conf
              hostPath:
                path: /etc/hccn.conf
            - name: weight-mount
              hostPath:
                path: /mnt/weight
            # --------TODO 5: 取消宿主机落盘挂载--------
            # - name: plog-path
            #   hostPath:
            #     path: /root/ascend/log
            #     type: DirectoryOrCreate
            # ------------------------------------------

            # --------TODO 6: 增加以下挂载路径--------
            - name: snapshot-weight
              hostPath:
                path: /mnt/snapshot/weight
                type: DirectoryOrCreate
            - name: dcmi
              hostPath:
                path: /usr/local/dcmi
            - name: ascend-driver
              hostPath:
                path: /usr/local/Ascend/driver
            - name: npu-smi
              hostPath:
                path: /usr/local/bin/npu-smi
            # ---------------------------------------
......
```
