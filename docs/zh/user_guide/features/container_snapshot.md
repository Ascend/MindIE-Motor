# 容器快照

## 特性介绍

Motor 服务框架可自动完成实例 Node 容器内 Device 快照保存以支持用户为推理实例制作容器快照；同时支持由快照恢复的实例向控制面自动注册，并自动完成实例 Node 容器内 Device 快照的恢复，实例从快照恢复后快速进入就绪状态。

使能容器快照功能后：

1. 框架可自动完成实例 Node 容器内 Device 快照的保存，使实例 Node 进入稳态点，等待用户制作实例 Node 容器快照镜像。用户可通过实例 Node Manager 心跳接口探测是否进入稳态点，使用 grus 工具制作实例Node容器的 Host 快照镜像。

2. 框架支持由快照恢复的实例向控制面自动注册，并可自动完成实例Node容器内 Device 快照的恢复。

容器快照可用于将实例快速恢复至就绪状态。Motor 服务框架默认将容器快照特性用于实例重调度场景。

## 环境约束

- **操作系统**：仅支持EulerOS R15C10 / HCE 3.0，且需要预装 CRIU 3.19 与 grus
- **容器运行时**：仅支持containerd

## 容器快照制作

启用容器快照特性后，Motor 服务框架可自动完成实例 Node 容器内 Device 快照保存以支持用户为推理实例制作容器快照。

当实例冷启动完成时，Motor 服务框架将自动保存 Device 快照：锁定 Device 状态，在容器内保存 Device 快照，并将模型运行时权重落盘，随后实例 Node 进入稳态点，等待用户制作实例 Node 容器快照镜像。

当 NodeManager 心跳接口返回 `true` 时，表示当前实例 Node 已进入稳态点。此时，用户可使用 grus 工具对实例 Node 容器执行 checkpoint，保存容器的 Host 快照镜像。

**实例 Node 容器快照组成**：落盘至宿主机的运行时模型权重，以及 Host 快照镜像（内含 Device 快照）。

## 启用制作容器快照配置

### 1. user_config.json

在 `user_config.json` 中增加容器快照相关配置组 `motor_container_snapshot_config`：

```json
"motor_container_snapshot_config": {
    "snapshot_mode": "FullSnapshot",
    "enable_snapshot": true,
    "snapshot_metadata_path": "/path/to/snapshot_metadata.json"
}
```

**enable_snapshot**：容器快照总开关，缺省为 `false`。该字段为 `false` 时，其余字段均不生效；为 `true` 时，表示启用实例 Node 容器的容器快照制作能力。

**snapshot_mode**：快照制作模式。当前仅支持 `FullSnapshot` 一种模式，即同时保存 Device 侧快照与 Host 侧快照。Motor 服务框架完成 Device 快照保存后，将等待用户使用 grus 工具对实例 Node 容器执行 checkpoint，以保存 Host 快照镜像。

**snapshot_metadata_path**：快照元数据文件路径（JSON 格式）。缺省为空字符串，默认容器快照特性用于 MindCluster 实例重调度，此时特性使用方为 MindCluster，快照元数据文件由 MindCluster 通过 ConfigMap 挂载。

非缺省时，表示当前容器快照特性由用户自定义应用场景，用户需将该文件挂载至容器内。文件中包含容器快照制作与恢复过程中所需的参数字段：

- **model_save_path**：仅在制作容器快照阶段使用。表示 Device 快照保存时，容器内运行时权重的落盘路径，须为宿主机挂载路径。
- **data_parallel_master_ip**：仅在恢复容器快照阶段使用。表示实例 Master DP 所在 Pod 的 IP；用户可不传入，由 Controller 决策后写入。
- **model_load_path**：仅在恢复容器快照阶段使用。表示 Device 快照恢复时，容器内运行时权重的加载路径，须为宿主机挂载路径。
- **job_name**：仅在恢复容器快照阶段使用。表示推理实例的唯一标识。
- **namespace**：仅在恢复容器快照阶段使用。表示推理服务的唯一标识。
- **checkpoint**：仅在制作容器快照阶段使用。用于指示 Host 侧快照是否保存完成。Host 侧快照保存完毕后，用户向快照元数据文件写入 `"checkpoint": "done"`，引擎将自动解锁 Device，并恢复当次冷启动的推理业务。

当用户指定 `snapshot_metadata_path` 时，表示当前容器快照特性由用户自定义应用场景，用户需自行挂载快照元数据文件；所有容器快照相关参数均优先以该文件中的配置为准。

## 容器快照应用场景

通过直接加载实例 Node 的容器快照，可快速将实例恢复至就绪状态。典型应用场景为实例重调度。

### 实例重调度

容器快照特性默认与实例重调度特性配合使用，可缩短实例重调度耗时，此时特性使用方为 MindCluster，快照元数据文件由 MindCluster 通过 ConfigMap 挂载，`snapshot_metadata_path` 字段可缺省或留空。

Motor 服务框架需为实例 Node 配置 Readiness 探针，供 MindCluster 判断实例 Node 是否进入稳态点。MindCluster 组件 NodedD 集成了 grus 工具，在实例 Node 进入稳态点后，执行 checkpoint 以保存实例Node容器的 Host 快照镜像。

**容器快照特性在实例重调度应用场景下的约束**：

- MindCluster 保存实例Node容器的 Host 快照镜像仅支持 CRD 部署方式。
- MindCluster 仅为同种实例保存一份容器 Host 快照镜像；例如 2P1D 场景下，仅为首个 P 实例保存容器 Host 快照镜像。
- 为便于管理实例的容器 Host 快照镜像，MindCluster 当前仅支持将 Host 快照镜像保存在集群共享存储路径下。

**Motor 服务框架侧需配置**：

在 `user_config.json` 中添加：

```json
"motor_container_snapshot_config": {
    "snapshot_mode": "FullSnapshot",
    "enable_snapshot": true
}
```

在 `infer_service_template.yaml` 中修改配置，以 Union 实例为例(具体yaml基准配置详见: MindIE-PyMotor/examples/deployer/yaml_template，此处仅表明改动点)：

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
                # 具体详见资料描述: MindIE-PyMotor/examples/features/pod_permission_guide/README.md
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
