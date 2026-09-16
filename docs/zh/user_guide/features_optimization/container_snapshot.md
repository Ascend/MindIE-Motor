# 容器快照特性

## 特性介绍

容器快照特性用于保存实例节点容器的运行状态，并在实例重调度等场景中快速恢复推理服务。推理引擎负责Device侧的suspend、resume；Motor服务框架负责快照前后刷新控制面状态（Controller域名、job_name、pod_ip）、准备快照元数据，并通过引擎状态感知保存/恢复是否完成。MindCluster或用户负责对实例节点容器执行Host侧checkpoint。
使用容器快照特性可显著缩短实例恢复至就绪状态的时间，减少重调度场景下的推理服务中断时长。

容器快照主要由以下两部分组成：

- 落盘至宿主机挂载路径的运行时模型权重。
- 容器Host快照镜像，其中包含Device快照状态。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 维度 | 约束内容 |
|------|----------|
| 硬件 | **内容缺失，需要人工补齐。** |
| 部署场景 | 仅支持containerd容器运行时。 |
| 引擎 | 推理引擎必须支持Device快照的保存与恢复能力，并提供对应的suspend和resume接口。 |
| 特性互斥 | **内容缺失，需要人工补齐。** |
| 软件依赖 | 操作系统仅支持EulerOS R15C10 / HCE 3.0，且需要预装CRIU 3.19与grus。 |
| 其他限制 | **内容缺失，需要人工补齐。** |

## 特性使用

### 环境准备

**内容缺失，需要人工补齐。**

### 使用场景

**依据原文上下文内容重组，请进行人工校验。**

#### 场景一：MindCluster 实例重调度（默认场景）

容器快照特性默认与MindCluster实例重调度配合使用，通过直接加载实例节点容器快照，可缩短实例恢复至就绪状态的时间。该场景下，快照元数据文件由MindCluster通过ConfigMap挂载，snapshot_metadata_path可缺省或留空。

Motor服务框架需为实例节点Pod配置Kubernetes Readiness Probe，供MindCluster查询实例节点是否到达稳态点。MindCluster在实例节点到达稳态点后执行checkpoint，保存容器Host快照镜像。

MindCluster侧的环境要求、组件部署和使用流程请参见《[容器快照部署及使用](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/04_usage/09_infer_operator_best_practice/06_container_snapshot_usage.md)》。

**容器快照特性在实例重调度应用场景下的约束**：

- MindCluster保存实例节点容器的Host快照镜像仅支持CRD部署方式。
- MindCluster仅为同种实例保存一份容器Host快照镜像；例如2P1D场景下，仅为首个P实例保存容器Host快照镜像。
- 为便于管理实例的容器Host快照镜像，MindCluster当前仅支持将Host快照镜像保存在集群共享存储路径下。

#### 场景二：用户自定义应用场景

用户自行创建并挂载快照元数据文件、查询稳态点、执行容器checkpoint，并管理Host快照镜像与运行时权重。

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

#### 场景一：MindCluster 实例重调度

**操作步骤**

1. 配置容器快照参数。
   在Motor服务框架侧user_config.json配置文件中添加motor_container_snapshot_config字段。

   ```json
   "motor_container_snapshot_config": {
      "enable_snapshot": true
   }
   ```

   **表 1** 配置参数说明

   | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
   |--------|------|----------|-----------|--------|------|
   | enable_snapshot | bool | true/false | 必填 | false | 容器快照总开关。<ul><li>false：其余字段均不生效；</li><li>true：表示启用实例节点容器的快照制作与恢复能力。</li></ul> |
   | snapshot_metadata_path | string | 容器内有效路径 | 选填 | 空字符串 | 快照元数据文件在容器内的路径。<ul><li>配置为空：进入默认应用场景（MindCluster实例重调度），元数据由MindCluster通过ConfigMap挂载，Node Manager将其复制到默认可写路径/snapshot/snapshot_metadata.json后使用。</li><li>配置非空：进入用户自定义应用场景,用户必须预先创建快照元数据文件，并将其挂载至配置指定的容器路径。</li></ul> |

   **表 2** 快照元数据（snapshot_metadata.json）字段说明

   | 字段 | 类型 | 取值范围 | 必填/选填 | 默认值 | 使用阶段 | 说明 |
   |------|------|----------|-----------|--------|----------|------|
   | model_save_path | string | 宿主机挂载路径 | 必填 | 无 | 快照制作 | 运行时模型权重的落盘路径 |
   | model_load_path | string | 宿主机挂载路径 | 必填 | 无 | 快照恢复 | 运行时模型权重的加载路径 |
   | job_name | string | 推理实例唯一标识 | 必填 | 无 | 快照恢复 | 恢复后注册时用于更新Node Manager的任务名 |
   | namespace | string | Kubernetes namespace | 选填 | 无 | 快照恢复 | 用于更新Controller DNS；非集群DNS场景可不配置 |
   | data_parallel_master_ip | string | Pod IP地址 | 选填 | 无 | 快照恢复 | 优先使用文件中的值，未配置时由Node Manager写入Controller下发值 |
   | checkpoint | string | "done" | 必填 | 无 | 快照制作 | Host侧checkpoint完成后写入，更新为"done"后引擎解锁Device |

2. 修改实例部署模板。
   在infer_service_template.yaml中完成以下配置（以Union实例为例，仅展示改动点；YAML基准配置请参见examples/deployer/yaml_template）：

   ```yaml
   ...
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
               # --------TODO 3: 启用readiness探针用于MindCluster探测稳态点--------
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
   ...
   ```

3. 制作容器快照。（**该步骤只讲了理论，没有具体的命令，是否需要补充**）
   1. 实例冷启动并进入健康状态后，引擎完成Device侧suspend：锁定Device状态、保存Device快照，并将运行时模型权重写入`model_save_path`。Node Manager仅准备快照元数据，不触发显存快照保存。
   2. 当本节点全部原生引擎Endpoint均完成suspend后，实例节点容器到达稳态点，通过Node Manager的`/readiness`返回`200`判断。
   3. 到达稳态点后，MindCluster使用grus对实例节点容器执行checkpoint，保存容器Host快照镜像。
      >[!NOTE] 说明
      >处于checkpoint过程中的实例无法提供推理服务。到达稳态点但checkpoint尚未完成时，Node Manager暂停向Controller上报正常心跳。
   4. Host侧checkpoint完成后，将元数据snapshot_metadata.json文件中的字段`checkpoint`更新为`"done"`。引擎检测到该状态后自行解锁Device，冷启动实例恢复提供推理服务。

4. 恢复容器快照。（**该步骤只讲了理论，没有具体的命令，是否需要补充**）
   1. MindCluster从Host快照镜像恢复实例节点容器，并挂载对应的运行时权重和快照元数据文件。
   2. Node Manager从元数据snapshot_metadata.json文件读取`job_name`和`namespace`，刷新Pod IP与Controller DNS，然后向Controller重新注册。
   3. Controller下发启动命令。Node Manager更新快照元数据snapshot_metadata.json文件中的`model_load_path`与`data_parallel_master_ip`字段；快照恢复场景继续使用容器快照中恢复的原生引擎进程。
   4. 引擎读取元数据snapshot_metadata.json文件中的`model_load_path`与`data_parallel_master_ip`完成resume。全部Endpoint恢复健康后，实例重新进入就绪状态。

#### 场景二：用户自定义应用场景

使用该场景时，用户必须预先创建快照元数据文件，并将其挂载至配置指定的容器路径。

- 制作容器快照前, 需要准备 model_save_path 字段；
- 从容器快照恢复前，需要准备 model_load_path 和 job_name；
- 使用集群内 Controller DNS 时还需准备 namespace。

1. 配置容器快照参数。
   在Motor服务框架侧user_config.json配置文件中添加motor_container_snapshot_config字段，且`snapshot_metadata_path`为非空路径。

   ```json
   "motor_container_snapshot_config": {
      "enable_snapshot": true,
      "snapshot_metadata_path": "/path/to/snapshot_metadata.json"
   }
   ```

   **表 3** 配置参数说明

   | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
   |--------|------|----------|-----------|--------|------|
   | enable_snapshot | bool | true/false | 必填 | false | 容器快照总开关。<ul><li>false：其余字段均不生效；</li><li>true：表示启用实例节点容器的快照制作与恢复能力。</li></ul> |
   | snapshot_metadata_path | string | 容器内有效路径 | 必填 | 空字符串 | 快照元数据文件在容器内的路径。<ul><li>配置为空：进入默认应用场景（MindCluster实例重调度），元数据由MindCluster通过ConfigMap挂载，Node Manager将其复制到默认可写路径/snapshot/snapshot_metadata.json后使用。</li><li>配置非空：进入用户自定义应用场景,用户必须预先创建快照元数据文件，并将其挂载至配置指定的容器路径。</li></ul> |

   **表 4** 快照元数据（snapshot_metadata.json）字段说明

   | 字段 | 类型 | 取值范围 | 必填/选填 | 默认值 | 使用阶段 | 说明 |
   |------|------|----------|-----------|--------|----------|------|
   | model_save_path | string | 宿主机挂载路径 | 必填 | 无 | 快照制作 | 运行时模型权重的落盘路径 |
   | model_load_path | string | 宿主机挂载路径 | 必填 | 无 | 快照恢复 | 运行时模型权重的加载路径 |
   | job_name | string | 推理实例唯一标识 | 必填 | 无 | 快照恢复 | 恢复后注册时用于更新Node Manager的任务名 |
   | namespace | string | Kubernetes namespace | 选填 | 无 | 快照恢复 | 用于更新Controller DNS；非集群DNS场景可不配置 |
   | data_parallel_master_ip | string | Pod IP地址 | 选填 | 无 | 快照恢复 | 优先使用文件中的值，未配置时由Node Manager写入Controller下发值 |
   | checkpoint | string | "done" | 必填 | 无 | 快照制作 | Host侧checkpoint完成后写入，更新为"done"后引擎解锁Device |

2. 制作容器快照。（**该步骤只讲了理论，没有具体的命令，是否需要补充**）

   1. 实例冷启动并进入健康状态后，引擎完成Device侧suspend。

   2. 当本节点全部原生引擎Endpoint均完成suspend后，实例节点容器到达稳态点。通过`/node-manager/status`返回`200 {"status": true}`判断。

   3. 到达稳态点后，用户使用grus对实例节点容器执行checkpoint，保存容器Host快照镜像。
      >[!NOTE] 说明
      >处于checkpoint过程中的实例无法提供推理服务。到达稳态点但checkpoint尚未完成时，Node Manager暂停向Controller上报正常心跳。
   4. Host侧checkpoint完成后，将元数据字段`checkpoint`更新为`"done"`。引擎检测到该状态后自行解锁 Device，冷启动实例恢复提供推理服务。

3. 恢复容器快照。（**该步骤只讲了理论，没有具体的命令，是否需要补充**）

   1. 用户从Host快照镜像恢复实例节点容器，并挂载对应的运行时权重和快照元数据文件。

   2. Node Manager从元数据读取`job_name`和`namespace`，刷新Pod IP与Controller DNS，向Controller重新注册。
   3. Controller下发启动命令。Node Manager更新快照元数据文件中的`model_load_path`与`data_parallel_master_ip`字段，快照恢复场景继续使用容器快照中恢复的原生引擎进程。
   4. 引擎读取元数据中的 model_load_path 与 data_parallel_master_ip 完成 resume。全部 Endpoint 恢复健康后，实例重新进入就绪状态。

### 验证特性

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

1. 验证容器快照制作是否完成：检查元数据文件中`checkpoint`字段值是否为`"done"`。

   ```bash
   cat /path/to/snapshot_metadata.json | grep checkpoint
   # 预期输出: "checkpoint": "done"
   ```

2. 验证实例是否从快照恢复成功：检查Node Manager状态接口返回。

   ```bash
   curl http://<pod-ip>:<port>/node-manager/status
   # 预期输出: 200 {"status": true}
   ```

3. 验证实例是否已重新就绪：检查Readiness Probe返回。

   ```bash
   curl http://<pod-ip>:<port>/readiness
   # 预期输出: 200 OK
   ```

## 常见问题

**内容缺失，需要人工补齐。**
