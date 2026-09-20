# 容器快照特性

## 特性介绍

容器快照用于保存实例节点容器的运行状态，并在后续创建实例或实例重调度时直接恢复推理服务，减少模型重新加载和初始化带来的等待时间。

容器快照由以下两部分组成：

- 运行时模型权重：由推理引擎保存到宿主机挂载目录。
- 容器 Host 快照镜像：保存容器进程及 Device 快照状态。

推理引擎负责 Device 侧的 suspend 和 resume；Motor 负责准备快照元数据、感知引擎状态，并在恢复后刷新 Controller 域名、任务名和 Pod IP 等控制面信息。Host 侧的 checkpoint 和 restore 由默认 K8s + MindCluster 部署环境或用户自行完成。

### 约束与限制

| 维度 | 约束内容 |
|------|----------|
| 硬件 | 支持 A3 和 A2。A2 容器快照只能在相同芯片代际的机器间共享，例如在 910B2 机器上制作的快照只能恢复到 910B2 机器。 |
| 自动管理模式 | 仅支持使用 containerd 容器运行时的默认 K8s + MindCluster CRD 部署模式。 |
| 推理引擎 | 必须支持 Device 快照保存与恢复，并提供 suspend 和 resume 接口，目前仅支持vLLM。 |
| 特性互斥 | 启用容器快照后不支持Node Manager配置热更新。 |
| 操作系统 | 支持 openEuler 24.03 及之后版本和 HCE 3.0，需预装openEuler定制版 CRIU 3.19。 |
| 存储 | 容器 Host 快照镜像保存目录不能挂载到容器内。运行时模型权重目录必须挂载到容器内，并在制作和恢复阶段保持可访问。 |

## 特性使用

### 环境准备

启用容器快照前，请完成以下准备：

1. 确认节点操作系统满足要求，并在节点上通过源码安装 openEuler 定制版 CRIU 3.19。

   1. 安装编译依赖：

      ```bash
      dnf --setopt=sslverify=false install -y \
          libcap-devel \
          libnet-devel \
          libnl3-devel \
          libselinux-devel \
          protobuf-c-devel \
          protobuf-devel \
          python3-devel \
          python3-protobuf \
          python3-wheel \
          xmlto
      ```

   2. 下载 openEuler 定制版 CRIU 3.19 源码并完成编译安装：

      ```bash
      git clone https://atomgit.com/src-openeuler/criu.git
      cd criu
      git checkout openEuler-24.03-LTS-SP4
      mkdir -p /root/rpmbuild/SOURCES/
      cd ..
      cp -r criu/* /root/rpmbuild/SOURCES/
      cd /root/rpmbuild/SOURCES/
      rpmbuild -bp criu.spec
      cd /root/rpmbuild/BUILD/criu-3.19
      vi criu/include/pipes.h # 单个容器内DP数较大时，制作快照时会发生pipe OOM, 需要修改增大pipes.h中的宏NR_PIPES_WITH_DATA至2048
      make GRUS=1 -j
      make npu_plugin -j
      cp /root/rpmbuild/BUILD/criu-3.19/criu/criu /usr/sbin/
      mkdir -p /usr/lib/criu/
      cp /root/rpmbuild/BUILD/criu-3.19/plugins/npu/npu_plugin.so /usr/lib/criu/
      ```

   3. 查看 CRIU 版本，确认安装成功：

      ```bash
      criu --version
      ```

      当命令输出的版本号为 `3.19` 时，表示安装成功。

2. 在默认 K8s + MindCluster 部署模式下，确认集群使用 containerd，并已安装支持容器快照的 MindCluster 组件及相关 CRD。MindCluster 侧的环境要求、组件部署和使用流程请参见《[容器快照部署及使用](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/04_usage/09_infer_operator_best_practice/06_container_snapshot_usage.md)》。
3. 准备保存容器 Host 快照镜像和运行时模型权重所需的宿主机存储目录。需要跨节点恢复时，请确保目标节点能够访问这些目录。
4. 使用其他部署模式时，需由部署平台或用户准备 Host 侧 CRIU 工具，例如GRUS、快照元数据文件及相应的容器挂载。

### 使用场景

**场景一：默认 K8s + MindCluster 部署模式**

- 适用场景：使用 containerd 和 MindCluster CRD 的标准 K8s 部署，需要缩短实例创建或重调度耗时。
- 特点：通过 `deploy.py` 启用容器快照后，部署环境自动完成快照配置、基准快照制作、缓存检测和快照恢复。
- 收益：首次冷启动并生成基准快照后，后续实例可直接从已有快照缓存恢复，减少模型权重加载和运行环境初始化耗时。

**场景二：其他部署模式**

- 适用场景：未使用默认 K8s + MindCluster 部署流程，但部署平台具备 Host 和 Device 侧快照能力，例如通过 ModelArts 平台部署 Motor。
- 特点：Motor 不提供非默认部署模式的容器快照自动管理能力，部署平台需自行集成快照元数据管理、容器挂载、快照文件维护以及 checkpoint 和 restore 流程。

### 使用样例

#### 默认 K8s + MindCluster 部署模式

用户需在 `user_config.json` 中配置：

```json
"motor_container_snapshot_config": {
    "enable_snapshot": true,
    "host_snapshot_image_path": "/shared/container-snapshots",
    "mnt_mount_path": "/mnt/runtime-data",
    "device_snapshot_weight_path": "/shared/snapshot-weights"
}
```

| 配置项 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `enable_snapshot` | bool | 是 | `false` | 容器快照总开关。设置为 `false` 时，其余字段不生效。 |
| `host_snapshot_image_path` | string | 是 | 无 | 容器 Host 快照镜像的宿主机保存目录，必须为绝对路径。快照在该目录下以当前服务的 namespace 命名。该目录不能挂载到容器内，也不能与 `mnt_mount_path` 相同或互为父子目录。 |
| `mnt_mount_path` | string | 是 | 无 | `mnt` 卷的宿主机路径及容器挂载路径，必须为绝对路径，并且不能与 `host_snapshot_image_path` 有交集。 |
| `device_snapshot_weight_path` | string | 是 | 无 | Device 运行时模型权重的宿主机保存目录，必须为绝对路径。快照权重在该目录下以模型权重名称、DP 数和 TP 数组合命名；容器内固定挂载到 `/snapshot/weight`。 |

通过 `deploy.py` 启动服务时，Motor 会完成以下配置：

- 基于通用部署模板渲染容器快照所需配置，并生成最终的 `infer_service.yaml`。
- 配置快照标签、Readiness Probe、所需的挂载项。
- 校验 `host_snapshot_image_path` 与 `mnt_mount_path` 不存在路径交集。

快照元数据由部署环境管理，`snapshot_metadata_path` 无需配置。启用容器快照后，部署环境按照以下规则保存和使用快照缓存：

- 容器 Host 快照镜像保存在 `host_snapshot_image_path` 下，并以当前服务的 namespace 命名。
- Device 快照权重保存在 `device_snapshot_weight_path` 下，并以模型权重名称、DP 数和 TP 数组合命名。

实例创建流程取决于当前服务是否已有快照缓存：

- 没有快照缓存：实例首先执行冷启动；到达快照稳态点后，部署环境制作并保存基准快照。
- 已有快照缓存：实例跳过完整的冷启动流程，直接加载容器 Host 快照镜像和对应的 Device 快照权重恢复启动。

每次创建或重新拉起服务时，部署环境都会检查 `host_snapshot_image_path` 下是否存在以当前 namespace 命名的快照，并据此选择冷启动或快照恢复启动。

> [!NOTE] 说明
> 首次制作基准快照时，实例仍需完成冷启动及 Device suspend。只有基准快照可用后，后续实例才能通过快照恢复启动。

<!-- -->
> [!WARNING] 警告
> 快照缓存与制作快照时使用的模型和并行配置绑定。更换模型，或修改 DP、TP 等并行配置后，如果需要重新制作快照，必须先删除 `host_snapshot_image_path` 下以当前 namespace 命名的原有快照缓存，再重新拉起服务并制作新快照。否则服务会命中旧缓存，并使用旧模型或旧并行配置对应的快照启动。`device_snapshot_weight_path` 下不再使用的旧快照权重可同步清理。

#### 其他部署模式

其他部署模式不会通过 `deploy.py` 自动生成部署 YAML 模板，也不会由该脚本执行 checkpoint、restore 或管理快照文件。部署平台需集成以下流程；部署平台未提供自动管理能力时，由用户自行完成，以下基于使用GRUS工具演示。

1. 创建快照元数据文件，并将其挂载到实例容器内。该文件必须是合法的 JSON 对象，初始内容可以为空：

   ```json
   {}
   ```

   快照元数据文件在容器内必须可读写。制作和恢复快照时使用的字段如下：

   | 字段 | 使用阶段 | 必填 | 说明 |
   |------|----------|------|------|
   | `model_save_path` | 快照制作 | 是 | 运行时模型权重的保存路径，必须指向容器内的宿主机挂载目录。 |
   | `model_load_path` | 快照恢复 | 是 | 运行时模型权重的加载路径，必须指向容器内的宿主机挂载目录。 |
   | `job_name` | 快照恢复 | 是 | 恢复后的推理实例唯一标识。Node Manager 使用该字段更新任务名。 |
   | `namespace` | 快照恢复 | 按需 | 使用集群内 Controller DNS 时填写 Kubernetes namespace；其他网络模式可以省略。 |
   | `controller_ip` | 快照恢复 | 按需 | 使用 IP 与 Controller 通信时填写恢复后 Controller 的 IPv4 或 IPv6 地址；使用集群内 Controller DNS 时可以省略。 |
   | `data_parallel_master_ip` | 快照恢复 | 否 | Data Parallel Master 的 IP 地址。未配置时，由 Node Manager 写入 Controller 下发的地址。 |
   | `checkpoint` | 快照制作 | 是 | Host 侧 checkpoint 完成后设置为 `"done"`，用于通知推理引擎解除 Device 锁定。 |

2. 在 `user_config.json` 中启用容器快照，并将 `snapshot_metadata_path` 配置为元数据文件在容器内的路径：

   ```json
   "motor_container_snapshot_config": {
       "enable_snapshot": true,
       "snapshot_metadata_path": "/snapshot/snapshot_metadata.json"
   }
   ```

3. 冷启动实例前，在快照元数据文件中配置运行时模型权重的保存路径：

   ```json
   {
       "model_save_path": "/snapshot/weight"
   }
   ```

4. 实例冷启动后，查询 Node Manager 状态，确认实例是否到达可执行 checkpoint 的稳态点：

   ```bash
   curl http://{instance-ip}:{node-manager-management-port}/node-manager/status
   ```

   当接口返回 HTTP 200 且响应为 `{"status": true}` 时，使用 GRUS 执行 Host 侧 checkpoint，并保存容器 Host 快照镜像：

   ```bash
   curl -sS -N --unix-socket /var/run/grus/grus.sock \
     -X POST "http://localhost/checkpoint" \
     -d "container_id={container-id}&location={host-snapshot-image-path}"
   ```

5. checkpoint 完成后，在快照元数据文件中将 `checkpoint` 设置为 `"done"`。推理引擎检测到该状态后解除 Device 锁定，实例恢复提供推理服务：

   ```json
   {
       "model_save_path": "/snapshot/weight",
       "checkpoint": "done"
   }
   ```

6. 需要从快照恢复容器时，在待恢复实例的 YAML 中配置 GRUS RuntimeClass、容器 Host 快照镜像路径和恢复标记。以下仅展示相关配置：

   ```yaml
   spec:
     template:
       spec:
         runtimeClassName: grus
         containers:
         - name: <container-name>
           env:
           - name: GRUS_SNAPSHOT_IMAGE_PATH
             value: "/path/to/host-snapshot-image"
           - name: GRUS_SNAPSHOT_RESTORED_FLAG
             value: "true"
   ```

7. 执行 restore 前，在快照元数据文件中补充恢复所需的信息：

   ```json
   {
       "model_save_path": "/snapshot/weight",
       "checkpoint": "done",
       "model_load_path": "/snapshot/weight",
       "data_parallel_master_ip": "x.x.x.x",
       "job_name": "<job-name>",
       "namespace": "<namespace>"
   }
   ```

   `namespace` 和 `controller_ip` 根据 Controller 通信方式选择配置：使用集群内 Controller DNS 时配置 `namespace`；使用 IP 通信时，将上述示例中的 `namespace` 替换为 `"controller_ip": "<controller-ip>"`。`data_parallel_master_ip` 可以省略，由 Node Manager 在收到 Controller 下发的地址后写入。

8. 容器恢复后，查询 Node Manager 状态：

   ```bash
   curl http://{instance-ip}:{node-manager-management-port}/node-manager/status
   ```

   当接口返回 HTTP 200 且响应为 `{"status": true}` 时，表示实例已恢复就绪。

> [!NOTE] 说明
> checkpoint 期间实例无法提供推理服务。在 `checkpoint` 更新为 `"done"` 前，Node Manager 不会上报正常心跳。

### 验证特性

#### 验证容器快照是否制作完成

在配置的 `host_snapshot_image_path` 下，进入以当前服务 namespace 命名的目录，检查每个实例的子目录中是否生成 `snapshot_status.json` 文件。目录结构如下：

```text
<host_snapshot_image_path>/<namespace>/<instance-name>/snapshot_status.json
```

例如，`host_snapshot_image_path` 配置为 `/path/to/host-snapshot`、namespace 为 `mindie-motor`、实例名称为 `vllm-0-decode` 时，对应文件为：

```text
/path/to/host-snapshot/mindie-motor/vllm-0-decode/snapshot_status.json
```

当前服务的每个实例目录下均生成 `snapshot_status.json`，表示容器快照制作完成。如果部分实例缺少该文件，请检查对应实例和 MindCluster infer-operator 的日志。

#### 验证实例是否从快照恢复成功

实例从快照创建后，查看实例日志，并依次检查 Host 侧容器恢复和 Device 侧恢复结果。

1. 检查日志中是否包含以下内容：

   ```text
   [snapshot] Node manager is restored from host side snapshot, registering...
   ```

   可以通过以下命令查询：

   ```bash
   kubectl logs -n <namespace> <instance-pod> | grep -i "restored from host side snapshot"
   ```

   日志中存在该内容，表示 Node Manager 已识别到当前容器由 Host 快照 restore。

2. 检查实例日志中以下内容的数量：

   ```text
   It took <time> seconds to resume.
   ```

   可以通过以下命令统计：

   ```bash
   kubectl logs -n <namespace> <instance-pod> | grep -c "It took .* seconds to resume"
   ```

   日志数量应等于该实例的 DP 数。Host 侧容器恢复日志存在，且 Device 侧打印了 DP 数量的 resume 成功日志，表示实例已从快照恢复成功。

首次启用且没有快照缓存时，实例会执行冷启动并制作基准快照，因此不会打印上述恢复日志。如果确认当前 namespace 已有快照缓存，但日志中仍未出现该内容，请检查 `host_snapshot_image_path` 下的快照名称是否与当前 namespace 一致、文件内容是否完整、MindCluster 的 infer-operator 组件是否正常运行，以及目标节点是否能够访问该快照。

#### 验证实例是否重新就绪

查看 Coordinator 日志，检查是否包含当前实例加入可用实例池的日志：

```text
Added instance ID <instance-id> (role: <role>, job_name: <job-name>) with <endpoint-num> endpoints to available pool successfully
```

可以通过以下命令查询：

```bash
kubectl logs -n <namespace> <coordinator-pod> | grep "Added instance ID"
```

日志中存在当前实例 `job_name` 对应的记录，表示该实例已经重新加入 Coordinator 的可用实例池并恢复就绪。

## 常见问题

### 配置快照路径时提示路径存在交集

**问题描述**

启用容器快照并启动服务时，提示 `host_snapshot_image_path` 与 `mnt_mount_path` 存在路径交集。

**原因分析**

`host_snapshot_image_path` 与 `mnt_mount_path` 配置为相同目录，或其中一个目录是另一个目录的父目录。容器 Host 快照镜像保存目录不能挂载到容器内，因此这两个路径不能存在交集。

**解决步骤**

1. 检查 `host_snapshot_image_path` 和 `mnt_mount_path` 的配置。
2. 将 `host_snapshot_image_path` 修改为不会挂载到容器内的独立宿主机目录，确保两个路径不相同且不互为父子目录。
3. 重新执行 `deploy.py` 启动服务。

### 更换模型或并行配置后仍加载旧快照

**问题描述**

更换模型或修改 DP、TP 等并行配置后重新拉起服务，实例仍然加载修改前的快照。

**原因分析**

服务启动时会优先查找 `host_snapshot_image_path` 下以当前 namespace 命名的快照。当前 namespace 的旧快照缓存未删除时，服务会直接使用旧模型或旧并行配置对应的快照启动。

**解决步骤**

1. 删除 `host_snapshot_image_path` 下以当前 namespace 命名的旧快照缓存。
2. 按需清理 `device_snapshot_weight_path` 下不再使用的旧快照权重。
3. 重新拉起服务，确认实例使用新模型和新并行配置完成冷启动并制作新的基准快照。
