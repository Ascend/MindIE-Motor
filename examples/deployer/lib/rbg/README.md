# RBG 部署模板与配置说明

本文以 Motor 的 RBG 适配实现、模板、云原生示例和部署 RBAC 已配套合入，且 deployer 镜像包含
对应代码为前提。Motor 行为以提交 `e12c6e7bc0c09a96f398b14004b6f8506fde2c4b` 为基线，
RBG 使用 v0.8.0，Ascend Volcano 调度策略按 mind-cluster v26.2.0 源码核对。
若所用分支尚未包含配套实现，应先准备已集成上述功能的源码和镜像，不能仅增加 `deploy_mode: rbg`。

将 `user_config.json` 的 `motor_deploy_config.deploy_mode` 设为 `"rbg"`，通过
`examples/deployer/deploy.py` 部署。本目录的渲染逻辑将 `yaml_template/rbg_template.yaml` 转换为一个
`workloads.x-k8s.io/v1alpha2` 的 `RoleBasedGroup`，并生成业务 Service、运行时 SA/RBAC 和
`motor-config` ConfigMap。协调策略与 HPA 由同一部署入口生成并提交。

## 模板中的角色

`rbg_template.yaml` 固定包含 9 个角色，未启用的角色保留在模板中、`replicas` 置 0：

| 角色 | 作用 | 默认实例形态 |
|------|------|--------------|
| `controller` | 实例生命周期管理 | `standalonePattern` |
| `coordinator` | 请求调度、推理入口、观测入口 | `standalonePattern` |
| `prefill` / `decode` | PD 分离的引擎角色 | `customComponentsPattern`（`size` 由实例 Pod 数决定） |
| `encode` | 多模态编码（可选） | `customComponentsPattern` |
| `union` | PD 混部（可选） | `customComponentsPattern` |
| `kv-store` | KV Store（可选） | `standalonePattern` |
| `kv-conductor` | KV 亲和（可选） | `standalonePattern` |
| `mf-store` | SGLang MF Store（可选） | `standalonePattern` |

默认引擎模板的每实例 Pod 数写入 `customComponentsPattern.components[0].size`。
引擎角色也支持在模板中使用 `leaderWorkerPattern`；此时写入其 `size`，该值包含 leader。
配置字段分别为 `single_p_instance_pod_num`、`single_d_instance_pod_num`、
`single_e_instance_pod_num` 和 `single_hybrid_instance_pod_num`。

默认引擎 Pod 名为 `{rbg_name}-{role}-{实例序号}-engine-{Pod序号}`；
`leaderWorkerPattern` 的 Pod 名为 `{rbg_name}-{role}-{实例序号}-{Pod序号}`。业务 Service 命名为
`{模板名}-{logical_service_name}-0-{role}`（`mf-store` 例外，保留模板名），例如
`mindie-motor-coordinator-obs-vllm-0-coordinator`。

## rbg_config 字段

RBG 的部署配置位于 `user_config.json` 的 `motor_deploy_config.rbg_config`，不是独立的
`rbg.config` 文件。下面集中展示当前 Motor 基线读取的六个顶层字段及协调策略的两类子策略。

### 完整 rbg_config 示例

以下为 PD 分离、未启用 DP 降级场景的配置片段。将其合并到对应硬件的完整 `user_config.json`，
保留原有实例数、Pod/NPU 数、镜像、权重、引擎和 KV 配置；不能用此片段替换整个文件。
全部显式填写是为了说明配置结构，实际部署可按下表省略可选项。

```json
{
  "motor_deploy_config": {
    "deploy_mode": "rbg",
    "job_id": "motor-dsv41",
    "scheduling_queue": "default",
    "rbg_config": {
      "name": "vllm",
      "logical_service_name": "vllm",
      "gang_scheduling": true,
      "scheduler_name": "volcano",
      "restart_policy_by_role": {
        "prefill": "RecreateRoleInstanceOnPodRestart",
        "decode": "RecreateRoleInstanceOnPodRestart"
      },
      "coordinated_policy": {
        "policies": [
          {
            "name": "engine-scaling",
            "roles": ["prefill", "decode"],
            "strategy": {
              "scaling": {
                "maxSkew": "100%",
                "progression": "OrderReady"
              },
              "rollingUpdate": {
                "maxSkew": 1,
                "partition": 0,
                "maxUnavailable": 1
              }
            }
          }
        ]
      }
    }
  }
}
```

### 字段类型、默认值与修改方式

下表中的字段均位于 `motor_deploy_config.rbg_config`，六个顶层字段均可省略：

| 字段 | 类型与默认值 | 作用与取值限制 |
|------|--------------|----------------|
| `name` | string；缺省等于 `logical_service_name` | RBG 名称、Pod 名前缀、原生 headless Service 的 `s-{name}-{role}` 前缀，以及 `rbg.workloads.x-k8s.io/group-name` 标签值。使用小写字母、数字和连字符，首尾为字母或数字，最多 63 字符；派生资源名还需满足长度校验 |
| `logical_service_name` | string；缺省 `vllm` | 业务 Service 名称中缀及实例业务标识，例如 `vllm-0`；采用与 `name` 相同的 DNS 名称格式。它与引擎的 `served_model_name` 用途不同，不必同名 |
| `gang_scheduling` | boolean；首次部署 `true`，更新时省略沿用线上值 | 是否创建 RBG 组级 PodGroup。启用时必须使用 `volcano`，且角色/组件不能存在依赖；不要填写字符串 `"true"` |
| `scheduler_name` | 非空 string；首次部署 `volcano`，更新时省略沿用线上值 | 所有角色 Pod 的 `schedulerName`。启用 Gang 或带 Ascend 硬件注解时必须为 `volcano`；更改此项不等于配置 RBG 控制器的 scheduler 参数 |
| `restart_policy_by_role` | object；省略的引擎角色通常为 `RecreateRoleInstanceOnPodRestart` | 键为已启用的 `prefill`、`decode`、`encode` 或 `union`；值仅允许 `None`、`RecreateRoleInstanceOnPodRestart`。`None` 关闭实例联动重建；满足 DP 降级条件的 Decode/Union 只能为 `None`，不能填 Controller、Coordinator 或未启用角色 |
| `coordinated_policy` | object；首次省略生成默认协调策略，更新时省略通常保留线上策略 | `policies` 必须为非空数组；规则名唯一、角色不得重复归属不同规则，且只能引用当前业务形态的引擎角色。`strategy` 至少包含 `scaling` 或 `rollingUpdate`，子项说明见[协调策略](#协调策略) |

示例中的三个外层字段位于 `motor_deploy_config`，不要放进 `rbg_config`：

| 字段 | 示例值 | 说明 |
|------|--------|------|
| `deploy_mode` | `rbg` | 选择 Motor RBG 部署入口，必须显式设置，否则默认走 InferServiceSet |
| `job_id` | `motor-dsv41` | 业务 namespace，采用上述 DNS 名称格式；部署前 namespace 必须已存在 |
| `scheduling_queue` | `default` | Volcano 队列名，需提前存在；首次省略时 Motor 不显式指定队列，更新时省略沿用线上组队列。仅在启用组调度时控制 RBG 组级 PodGroup 的队列 |

`rollingUpdate` 是可选的跨角色滚动更新协调；不需要时可删除整个 `rollingUpdate` 对象，
默认协调策略只包含 `scaling`。这组参数不负责启用 HPA；HPA 由 `user_config.json` 顶层的
`scaling_policy` 配置，见[扩缩容](#扩缩容)。不要在 `rbg_config` 中填写 `sp-block`、硬件策略、
Pod/NPU 数或 `baseDelaySeconds` / `maxDelaySeconds`；重启退避时间来自 RBG YAML 模板，不由此对象读取。

修改 Gang、scheduler、queue 或活动引擎的重启策略，需要清理后重新部署；修改 RBG 名称、业务名称或
namespace 时也应按新部署处理。协调策略通过正常部署命令重新提交，不能通过 `--update_config` 或
`--update_scaling_policy` 修改。配置源码见本文 Motor 基线的 `lib/rbg/config.py`、`render.py` 和 `scaling.py`。

DP 降级条件包括 Controller 开启容错和 `enable_dp_scale_down`，且对应 Decode/Union 引擎启用容错。
`restart_policy_by_role` 使用完整角色名作为键，例如 `decode`。

实例数与副本的对应关系：

- `p_instances_num`、`d_instances_num`、`e_instances_num`、`hybrid_instances_num`
  → `spec.roles[].replicas`
- `single_p_instance_pod_num` / `single_d_instance_pod_num` / `single_e_instance_pod_num` /
  `single_hybrid_instance_pod_num` → 每个实例的 Pod 数
- `p_pod_npu_num` / `d_pod_npu_num` / `e_pod_npu_num` / `hybrid_pod_npu_num` → 每个引擎 Pod 的 NPU 数量

## 前置组件安装

Motor 的 `deploy_mode: rbg` 依赖 RBG v0.8.0 控制器、配套 CRD 和 Volcano。Motor 的部署 Chart
和 `deploy.py` 不负责安装这些组件，需在部署 Motor 前单独完成。

### 安装 Volcano

Ascend NPU 场景建议使用 MindCluster 提供的 Volcano 软件包（包含 `vc-scheduler`、
`vc-controller-manager` 和 Ascend-volcano-plugin）。完整步骤见
[MindCluster Volcano 安装指导](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/05_developer_guide/00_installation_deployment/00_manual_installation/05_volcano.md)，
仓库内的[环境准备](../../../../docs/zh/user_guide/environment_preparation.md)也引用了同一章节。

最小流程：

```bash
# 1. 确认管理节点已有 Volcano 镜像
docker images | grep volcanosh

# 2. 将 Volcano 软件包中的 volcano-v{version}.yaml 放到管理节点任意目录
#    单机或未安装 ClusterD 时，先在 YAML 的 scheduler 启动参数中设置：
#    "useClusterInfoManager": "false"
kubectl apply -f volcano-v{version}.yaml

# 3. 确认 volcano-scheduler 和 volcano-controllers 已运行
kubectl get pods -n volcano-system
```

如果镜像不存在，请按 MindCluster 的“准备镜像”章节制作或导入；Volcano 版本和镜像 tag
以 MindCluster 配套版本为准（示例为 `v1.9.0`，Ascend 镜像 tag 形如 `v1.9.0-v26.x.x`）。
非 Ascend NPU 场景也可以按开源 Volcano 官方 Helm Chart 安装，具体命令以开源 Volcano
官方文档为准；Ascend NPU 调度需要 MindCluster 的 Ascend-volcano-plugin，不建议直接用
开源 Chart 替代。

### 安装 RBG v0.8.0

要求 Kubernetes >= 1.28，并且已完成上一步的 Volcano 安装；使用 Helm 安装时还需 Helm 3。
以下命令在 Linux Motor 源码根目录执行。先准备两种安装方式共用的 RBG 源码，
将 `RBG_SOURCE_DIR` 替换为尚未存在的绝对目录：

```bash
RBG_SOURCE_DIR=/tmp/rbg-v0.8.0
git clone --branch v0.8.0 --single-branch https://github.com/sgl-project/rbg.git "$RBG_SOURCE_DIR"
```

然后选择 Helm 或 kubectl 安装。两种方式均使用上述绝对路径，无需执行 `cd rbg`，
安装后当前目录仍为 Motor 源码根目录。

使用 Helm 安装：

```bash
helm upgrade --install rbgs "$RBG_SOURCE_DIR/deploy/helm/rbgs" \
  --create-namespace \
  --namespace rbgs-system \
  --set controller.features.gangScheduling.schedulerName=volcano \
  --wait
```

RBG Chart 的 `controller.features.gangScheduling.schedulerName` 默认是
`scheduler-plugins`；使用 Volcano 时必须显式设置为 `volcano`。Chart 默认通过 CRD Upgrader
Job 安装或升级配套 CRD。

也可以使用 kubectl 清单安装：

```bash
kubectl apply --server-side -f "$RBG_SOURCE_DIR/deploy/kubectl/manifests.yaml"

# kubectl 清单默认使用 scheduler-plugins，需要补充 Volcano 参数
kubectl -n rbgs-system patch deploy rbgs-controller-manager --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--scheduler-name=volcano"}]'

kubectl wait deploy/rbgs-controller-manager -n rbgs-system \
  --for=condition=available --timeout=5m
```

生产环境优先使用 Helm，避免手工 patch 在升级时被覆盖。

### 验证安装

```bash
# RBG 控制器和 Pod
kubectl -n rbgs-system get deploy rbgs-controller-manager
kubectl -n rbgs-system get pods -l control-plane=rbgs-controller

# 控制器必须使用 volcano
kubectl -n rbgs-system get deploy rbgs-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[0].args}'

# CRD 必须存在，并包含 v1alpha2
kubectl get crd | grep workloads.x-k8s.io
kubectl api-resources | grep workloads.x-k8s.io
```

期望控制器参数中包含 `--scheduler-name=volcano`，并存在以下 CRD：

- `rolebasedgroups.workloads.x-k8s.io`
- `coordinatedpolicies.workloads.x-k8s.io`
- `roleinstancesets.workloads.x-k8s.io`
- `roleinstances.workloads.x-k8s.io`
- `rolebasedgroupscalingadapters.workloads.x-k8s.io`

`volcano-system` 用于 Volcano，`rbgs-system` 用于 RBG 控制器；Motor 的 RBG 业务负载部署在
`motor_deploy_config.job_id` 对应的业务 namespace 中，每个业务 namespace 只部署一套 RBG。

## 前置条件

以下为部署前检查项，安装步骤见上一节 [前置组件安装](#前置组件安装)。

- 已安装并运行 RBG v0.8.0 控制器及配套 CRD。部署流程检查的 `rolebasedgroups`、`coordinatedpolicies`、
  `roleinstancesets`、`roleinstances`、`rolebasedgroupscalingadapters` 均需提供 `v1alpha2` 版本。
- 默认启用 Volcano 组调度，集群需已安装 Volcano。
  RBG 控制器需设置 `--scheduler-name=volcano`，Motor 的 `rbg_config.scheduler_name` 也需为 `volcano`。
  使用 RBG 自身的 Helm Chart 时，控制器配置如下；该字段的默认值为 `scheduler-plugins`：

  ```yaml
  controller:
    features:
      gangScheduling:
        schedulerName: volcano
  ```

- 生产部署前配置推理 TLS 与管理面 API Key，保留安全预检，具体字段见
  [Deployer 安全预检](../../README.md#安全预检)。`--skip-check` 仅限隔离的本地或测试环境验证，
  生产环境不得使用。
- 部署账号需具备下文列出的资源权限。RBG 控制器使用自身的账号和权限，需单独安装配置。

以上控制器 Helm 配置属于 RBG 安装配置；Motor 的部署 Chart 不负责安装 RBG 或 Volcano。

### 部署账号 RBAC

[云原生 ClusterRole](../../../cloud_native_deploy/k8s/clusterrole.yaml) 和
[Helm RBAC 模板](../../../cloud_native_deploy/helm/templates/rbac.yaml.tpl) 需与 RBG 适配实现配套更新。
本文 Motor 基线包含 RBG、CoordinatedPolicy、ScalingAdapter 与 HPA 权限；所用分支或镜像仍需
逐项核对，不能据此假定文档分支中的 RBAC 已同步，也不能只增加一条 RBG `list` 权限。
应检查实际部署账号绑定的 ClusterRole，确保以下权限完整存在：

| API 组 | 资源 | 动词 | 用途 |
|--------|------|------|------|
| `workloads.x-k8s.io` | `rolebasedgroups` | `get`、`list`、`create`、`patch`、`delete` | 部署、配置和实例数更新、清理；热更新 HPA 策略还会列举受管 RBG |
| `workloads.x-k8s.io` | `coordinatedpolicies` | `get`、`create`、`patch`、`delete` | 协调策略的创建、更新与清理 |
| `workloads.x-k8s.io` | `roleinstances`、`roleinstancesets`、`rolebasedgroupscalingadapters` | `get`、`list` | 查询实例、Adapter，确认清理结果和自动扩缩容状态 |
| `workloads.x-k8s.io` | `rolebasedgroupscalingadapters/scale` | `get` | 启用 HPA 前读取 Adapter 的 `scale` 子资源 |
| `autoscaling` | `horizontalpodautoscalers` | `get`、`list`、`create`、`patch`、`delete` | HPA 的部署、启停、策略更新与清理 |
| 核心组 `""` | `configmaps` | `get`、`list`、`watch`、`create`、`patch`、`delete` | 运行时配置的创建、更新、清理，以及运行时读取 |
| 核心组 `""` | `serviceaccounts` | `get`、`create`、`patch`、`delete` | 运行时账号的创建、更新与清理 |
| 核心组 `""` | `services` | `get`、`list`、`create`、`patch`、`delete` | 业务 Service 管理和端口检查 |
| 核心组 `""` | `pods` | `get`、`list`、`watch` | 查询 Pod、确认退出，以及运行时通过 `POD_NAME` 读取自身 Pod |
| 核心组 `""` | `persistentvolumeclaims` | `get`、`list`、`create` | 创建或复用数据卷，清理前检查归属；不删除数据 PVC |
| 核心组 `""` | `namespaces` | `get` | 检查目标 namespace 已存在 |
| 核心组 `""` | `nodes` | `get`、`list`、`watch` | 查询硬件节点标签和超节点拓扑 |
| `apiextensions.k8s.io` | `customresourcedefinitions` | `get` | 检查 RBG 配套 CRD 的 `v1alpha2` 版本 |
| `apps` | `deployments`、`statefulsets` | `get` | 清理前核对其他 HPA 目标工作负载 |
| `rbac.authorization.k8s.io` | `clusterroles` | `get`、`create`、`patch`、`delete` | 管理运行时角色 |
| `rbac.authorization.k8s.io` | `clusterrolebindings` | `get`、`list`、`create`、`patch`、`delete` | 管理运行时绑定并检查共享引用 |
| `rbac.authorization.k8s.io` | `rolebindings` | `list` | 跨 namespace 检查运行时 ClusterRole 是否被其他绑定引用 |

部署、手动扩缩容及清理的调用见 `lib/rbg/lifecycle.py` 和 `lib/rbg/scaling.py`；
热更新 HPA 策略见 `lib/scaling_policy_updater.py`。该更新入口读取 `motor-config`，
列举受管 RBG 和 HPA，再对 HPA 执行 `patch`，因此必须同时具备 ConfigMap `get`、
RBG `list` 以及 HPA `list/patch` 权限。不能用一条 `rolebasedgroups` 规则代替上述完整权限配置。

表中 `configmaps`、`pods`、`nodes` 的 `watch`（以及 `pods`、`nodes` 的 `get`）来自
`rbg_template.yaml` 生成的运行时 ClusterRole，由控制器运行时使用。部署账号在集群范围创建该
ClusterRole 时也必须具备对应的集群范围权限，否则会被 Kubernetes 的 RBAC 提权限制拒绝。
应核对实际应用的 `k8s/clusterrole.yaml` 或 Helm RBAC，自定义最小权限账号时不要遗漏。

## 调度规格限制

以下为当前 Motor RBG 适配器的明确限制，校验实现见
[`validate_rbg_gang_configuration`](config.py)：

| 配置或规格 | 当前行为 |
|------------|----------|
| `huawei.com/schedule_policy: *-sp` 超节点策略 | 当前 Motor RBG 适配器在渲染时直接拒绝；关闭 `gang_scheduling` 也不能使用。此为适配器限制，不代表 Ascend Volcano 不支持这些策略 |
| 同一 RBG 中，带硬件调度注解的活动角色使用不同的 `(sp-block, huawei.com/schedule_policy)` 组合 | 启用组调度时拒绝，当前适配器不能用一个 RBG PodGroup 保留不同的实例硬件调度策略 |
| 活动角色使用不同的 `schedulerName` | 拒绝；存在 `sp-block` 或硬件调度策略时还必须使用 `volcano` |
| 角色串行创建、角色依赖或组件 `depends-on` 依赖 | 启用组调度时拒绝；角色需使用 `podManagementPolicy: Parallel` |

从 InferServiceSet 迁移时，应检查各 `motor_engine_*_config.additional_annotations` 和自定义模板中的
`huawei.com/schedule_policy`、`sp-block`。依赖上述不支持规格的部署，不能仅切换 `deploy_mode` 后沿用。
关闭组调度仅会放宽部分校验，源码校验通过不能作为该硬件部署方案已验证的依据。

队列通过 `motor_deploy_config.scheduling_queue` 设置：Motor 的 [`render.py`](render.py) 将其写入 RBG 的
`rbg.workloads.x-k8s.io/group-gang-scheduling-volcano-queue` 注解，与 RBG v0.8.0 的
[`Volcano PodGroupManager`](https://github.com/sgl-project/rbg/blob/v0.8.0/pkg/scheduler/volcano/manager.go)
读取的键一致；启用组调度时，组级 `PodGroup.spec.queue` 会使用该队列。使用前仍需确认目标 Volcano
队列存在，且 `scheduler_name` 为 `volcano`；关闭组调度时不创建该组级 PodGroup。
组调度、调度器或 `scheduling_queue` 的变更不允许在线修改，需要清理后重新部署。

### `sp-block` 与超节点调度

mind-cluster v26.2.0 实现了 A3、A5 的超节点调度及 `sp-block` 处理；是否启用这些能力，
取决于实际选择的策略、作业资源、拓扑和部署适配。不能把当前 RBG 的接入限制概括为硬件或调度器不支持。

**Ascend Volcano 的策略选择与分块语义：**

- `get910CardHandlerName()` 优先采用有效的 `huawei.com/schedule_policy`；没有有效显式策略、
  但存在 `sp-block` 时，才通过 `getA3HandlerNameWithSpBlock()` 选择 A3 超节点处理器。
- A3 的 `sp-block` 指定一个逻辑超节点的 NPU 数，`checkSpBlock()` 将其换算成节点数，
  `selectSuperPodForJob()` 按物理超节点拓扑选择逻辑块。本例 P/D 各为一个 16 NPU Pod，
  `sp-block=16` 对应每块一个节点，两个逻辑块不保证来自同一物理超节点；这不表示 `sp-block` 没有亲和作用。
- A5 默认注入的 `chip8-node8` 映射到 `module910bx8Name`，是节点内互联策略。
  有效显式策略优先，因此同时生成 `sp-block=8` 也不会自动启用 A5 超节点处理器。
  A5 的 `chip8-node8-ra64-sp` 处理器则会读取和校验 `sp-block`，并结合框亲和参数 `ra-block` 调度。

相关策略与硬件的对应关系如下；本表说明调度器能力，不表示当前 Motor RBG 适配器允许配置这些策略：

| `huawei.com/schedule_policy` | 对应场景 |
|-----------------------------|----------|
| `chip2-node16-sp` | A3 超节点，每节点 16 NPU |
| `chip2-node8-sp` | A3 超节点，每节点 8 NPU |
| `chip8-node8` | 节点内 8 卡互联，非超节点策略 |
| `chip8-node8-sp` | Atlas 850／850E 超节点 |
| `chip8-node8-ra64-sp` | Atlas 950 SuperPoD 超节点 |
| `chip8-node16-sp` | Atlas 950 SuperPoD Flex |

**当前 Motor RBG 的适配边界：**

- `validate_rbg_gang_configuration()` 直接拒绝 `*-sp`。Ascend Volcano 的 A5 超节点处理器同时存在
  普通作业的 `selectNodesForJob()` 和 InferService 的 `selectNodesForInferService()` 路径；
  InferService 跨实例亲和需要相应身份信息，不能据此推断所有超节点调度都依赖 infer-operator 身份注入。
- RBG 的 Gang 调度创建组级 PodGroup，并通过 `scheduling.k8s.io/group-name` 绑定 Pod，
  不自动生成 P/D 同物理超节点约束。关闭 Gang 也不会移除 Motor 注入的硬件注解；存在这些注解时，
  Motor 校验仍要求 `scheduler_name=volcano`。
- 还需核实组级 NPU 识别与硬件注解传递。RBG v0.8.0 的 `PodGroupManager` 未设置 `spec.minResources`；
  Ascend v26.2.0 的 `GetVCJobReqNPUTypeFromJobInfo()` 从该字段或 PodGroup 的
  `volcano.sh/schedule.plugin` 识别 NPU 作业，而本文 Motor 基线未注入后者。
  `getJobAnnotationFromJobInfo()` 回退读取 Pod 注解时也只取一个 Task，组内同时包含非 NPU 角色。
  因此，P/D 的注解一致或 Motor 校验通过，均不足以证明硬件策略已在组级可靠生效；
  需要完成接入核对和实际调度验证，不能仅修改注解或关闭 Gang 就宣称问题已解决。

若要求 P/D 位于同一指定超节点，可以为 `prefill_node_selector` / `decode_node_selector` 设置
相同且准确标识该超节点成员的标签，作为静态节点范围约束。这不会补齐上述调度接入，
也不能替代硬件策略所需的真实拓扑信息；应分别验证节点归属、硬件调度和业务推理。

源码依据（Ascend 路径均相对于 mind-cluster v26.2.0 的 `component/ascend-for-volcano/`）：

- `internal/npu/factory.go`、`common/util/constants.go`：策略优先级及硬件对应关系。
- `internal/npu/ascend910/ascend910a3/superpod/frame.go`：A3 分块校验和节点选择。
- `internal/npu/policy/chip8node8ra64sp/job.go`、`frame.go`、`infer_service.go`：A5 分块及两类作业路径。
- `plugin/job.go`：组级 NPU 识别和注解读取。
- RBG v0.8.0 的 `pkg/scheduler/volcano/manager.go`：PodGroup 创建及 Pod 绑定。
- 本文 Motor 基线的 `lib/rbg/config.py`、`pods.py`、`render.py`：适配器校验与注解生成。

## A2、A3、A5 配置调整

以匹配模型、引擎和硬件的现有 `user_config.json` 为起点，三类硬件共同需要核对：

1. 将 `motor_deploy_config.deploy_mode` 设为 `"rbg"`；需要指定名称时，在同一对象内添加
   `rbg_config.name` / `logical_service_name`。首次部署缺省启用 Volcano 组调度，默认值见上文。
2. 在 `motor_deploy_config` 中设置实际的 `image_name`、`job_id`、`weight_mount_path`，并核对
   `motor_engine_prefill_config` / `motor_engine_decode_config` 中的模型路径、并行配置和引擎参数。
3. 若原配置显式启用了 `motor_controller_config.fault_tolerance_config.enable_scale_p2d`，将其改为
   `false`；若启用了 `motor_container_snapshot_config.enable_snapshot`，也需改为 `false`。
   两项均不支持，校验分别见 [`config.py`](config.py) 与 [`render.py`](render.py)。
4. 核对每个引擎角色的实例数、每实例 Pod 数和每 Pod NPU 数，并按上一节检查调度注解。
   `motor_deploy_config.prefill_node_selector` / `decode_node_selector` 可附加业务标签，
   不能覆盖生成器要求的硬件标签，校验见 [`pods.py`](pods.py)。
   硬件网络环境变量继续使用相应的 `env.json`；RBG 适配不自动将 A3 通信配置转换为 A2/A5 配置。

以下以仓库现有 DeepSeek-V4.1-Flash vLLM 典配展示差异；数值取自所链接的原文件，迁移后仍需检查调度限制：

| 硬件及配置来源 | `hardware_type` | P/D 实例数 | 每实例 Pod 数（P/D） | 每 Pod NPU 数（P/D） | P DP×TP×PP | D DP×TP×PP | KV 配置 |
|----------------|-----------------|------------|----------------------|----------------------|------------|------------|---------|
| [A2 配置](../../../../model_configs/vllm/models/deepseek_v4.1_flash/A2/user_config.json) | `800I_A2` | 1 / 1 | 2 / 2 | 8 / 8 | 4×4×1 | 8×2×1 | mooncake |
| [A3 配置](../../../../model_configs/vllm/models/deepseek_v4.1_flash/A3/user_config.json) | `800I_A3` | 1 / 1 | 1 / 1 | 16 / 16 | 4×4×1 | 8×2×1 | memcache standalone |
| [A5 配置](../../../../model_configs/vllm/models/deepseek_v4.1_flash/A5/user_config.json) | `Ascend950` | 1 / 1 | 1 / 1 | 8 / 8 | 8×1×1 | 8×1×1 | memcache inprocess |

每个实例的 `Pod 数 × 每 Pod NPU 数` 与其 `DP × TP × PP` 在上述典配中均相等：
A2、A3 的 P/D 每实例均为 16 NPU，A5 均为 8 NPU。初始 P+D 总申请量分别为 32、32、16 NPU。
每个 Pod 的 NPU 必须由同一 Kubernetes Node 提供；不能把多个节点的零散资源拼成一个 Pod。

引擎和通信参数也必须随硬件成套替换：

| 参数（P / D） | A2 | A3 | A5 |
|---------------|----|----|----|
| `quantization` | `ascend` / `ascend` | `ascend` / `ascend` | `deepseek_v4_fp8` / `deepseek_v4_fp8` |
| `max-num-batched-tokens` | 4096 / 512 | 8192 / 512 | 5120 / 1024 |
| `max-num-seqs` | 16 / 72 | 16 / 72 | 16 / 64 |
| MooncakeHybridConnector 的 `kv_port` | `30000` / `30100` | `30000` / `30100` | `26010` / `27010` |
| `HCCL_OP_EXPANSION_MODE` | `AIV` / 未显式设置 | `AIV` / 未显式设置 | `CCU_SCHED` / `CCU_SCHED` |
| `HCCL_BUFFSIZE` | 1024 / 1800 | 1024 / 4096 | 1024 / 1600 |
| `HCCL_CONNECT_TIMEOUT` | 1200 / 1200 | 1200 / 1200 | 1200 / 1200 |
| `HCCL_EXEC_TIMEOUT` | 204 / 204 | 未显式设置 / 未显式设置 | 2040 / 2040 |
| `MOTOR_ENABLE_IPC_HOST`（公共环境） | 1 | 1 | 1 |

“未显式设置”表示原 `env.json` 中没有该项，不应自行补成其他硬件的取值。表中的端口是对应
connector 内的初始值，不要将 A5 端口改回 A2/A3 的值；运行时端口分配仍遵循 NodeManager 配置。

RBG v0.8.0 的 `api/workloads/v1alpha2/helper.go:GetGroupSize()` 对 `customComponentsPattern`
只计角色 `replicas`，未乘组件的 `size`。因此 A2 的 P/D 实际合计 4 个引擎 Pod，却只按 2 个计入
组级 `minMember`；还应加上实际启用的控制和 KV 等角色。A2 的资源典配数值正确，但不能将当前
组级 Gang 阈值视为覆盖全部 Pod；使用多 Pod 实例时需先核实或补齐该计数，不能为绕过限制缩减典配资源。
A3/A5 当前 P/D 均为单 Pod，此项不会造成引擎 Pod 少计，但仍需满足前文的硬件调度接入条件。

硬件字段处理见 [`pods.py` 的 `_hardware`](pods.py) 和
[`generator/engine.py`](../generator/engine.py)：

- **A2**：按 A2 典配设置 `hardware_type` 和上述 Pod/NPU 数。生成器设置
  `accelerator=huawei-Ascend910` 与集群对应的 `accelerator-type`，资源名为 `huawei.com/Ascend910`，
  不自动添加 `sp-block`。
- **A3**：使用 A3 典配的硬件和数量字段，资源名同 A2；未手工覆盖注解时，自动设置
  `sp-block=每实例 Pod 数 × 每 Pod NPU 数`。DeepSeek-V4.1-Flash A3 的 P/D 均为单 Pod，
  自动得到 `sp-block=16/16`，不会触发默认组调度的 P/D 硬件策略不一致校验；仍需核实上一节的
  RBG 调度接入，并先修正下文列出的 Prefill MultiConnector 层级问题，不能据此认定可直接部署。
  其他典配仍可能不一致，例如 DeepSeek-V3.1 A3 的 P=1、D=2 会得到 `16/32`，默认组调度会拒绝；
  迁移前必须核对 P/D 的 `sp-block` 是否一致，不能仅为绕过校验随意修改 Pod/NPU 数。
- **A5**：上表典配的 `hardware_type` 为 `Ascend950`，需使用对应的 A5 Pod/NPU 数和引擎配置。
  生成器设置 `accelerator=huawei-npu`，移除 `accelerator-type`，资源名为 `huawei.com/npu`，
  按相同公式设置 `sp-block`，并默认设置 `huawei.com/schedule_policy: chip8-node8`。
  `chip8-node8` 是节点内互联策略，不会因为同时存在 `sp-block` 而变成超节点策略。
  Atlas 950 SuperPoD 与 Flex 分别对应 `chip8-node8-ra64-sp` 和 `chip8-node16-sp`，
  当前适配器拒绝 `*-sp`；调度器能力与 RBG 接入边界见上一节
  [`sp-block` 与超节点调度](#sp-block-与超节点调度)。
  需要 PD 异构芯片选择时，分别核对 `motor_engine_prefill_config.npu_chip_name` 与
  `motor_engine_decode_config.npu_chip_name`；其值会写入 `huawei.com/npu.chip.name` 节点选择标签，
  配置入口见 [`apply_pd_heterogeneous_node_selector`](../generator/engine.py)。

云原生 RBG 示例的 `hardware_type` 为 `800I_A3`、P/D 均为单 Pod、每 Pod 16 NPU。
使用 A2/A5 或其他模型时，需要替换对应的硬件、数量、引擎配置及 `env.json`，不能只修改硬件名称。

### A5 DeepSeek-V4.1-Flash 部署参数

A5 的完整配置位于 `model_configs/vllm/models/deepseek_v4.1_flash/A5/`，关键参数如下：

| 字段 | A5 取值 |
|------|---------|
| `motor_deploy_config.hardware_type` | `Ascend950` |
| `p_instances_num` / `d_instances_num` | `1` / `1` |
| `single_p_instance_pod_num` / `single_d_instance_pod_num` | `1` / `1` |
| `p_pod_npu_num` / `d_pod_npu_num` | `8` / `8` |
| Prefill `data_parallel_size` / `tensor_parallel_size` / `pipeline_parallel_size` | `8` / `1` / `1` |
| Decode `data_parallel_size` / `tensor_parallel_size` / `pipeline_parallel_size` | `8` / `1` / `1` |
| RBG 渲染的 P/D `sp-block` | `8` / `8`（生成值，不代表超节点调度已启用） |
| 默认 `huawei.com/schedule_policy` | `chip8-node8`（非超节点策略） |
| P/D `quantization` | `deepseek_v4_fp8` |
| KV | `memcache` + `inprocess` |
| `motor_common_env.MOTOR_ENABLE_IPC_HOST` | `1` |
| P/D `HCCL_OP_EXPANSION_MODE` | `CCU_SCHED` |
| P/D `HCCL_BUFFSIZE` | Prefill `1024`，Decode `1600` |
| P/D `HCCL_CONNECT_TIMEOUT` / `HCCL_EXEC_TIMEOUT` | `1200` / `2040` |
| NPU 总数（P+D） | `16` |

以下引擎参数位于 P/D 各自的 `engine_config`，应从 A5 完整典配保留；不要只替换部署资源字段：

| `engine_config` 参数 | Prefill | Decode |
|----------------------|---------|--------|
| `data_parallel_rpc_port` | 9000 | 9000 |
| `served_model_name` | `dsv41` | `dsv41` |
| `enable_expert_parallel` | `true` | `true` |
| `max-model-len` | 1048576 | 1048576 |
| `max-num-batched-tokens` | 5120 | 1024 |
| `max-num-seqs` | 16 | 64 |
| `gpu_memory_utilization` | 0.9 | 0.92 |
| `block-size` | 128 | 128 |
| `quantization` | `deepseek_v4_fp8` | `deepseek_v4_fp8` |
| `tokenizer-mode` / `tool-call-parser` / `reasoning-parser` | 均为 `deepseek_v41` | 均为 `deepseek_v41` |
| `trust-remote-code` / `enable-auto-tool-choice` / `enable-prefix-caching` | 均为 `true` | 均为 `true` |
| `engram-config.cpu_offload` / `engram-config.dp_shared_memory` | 均为 `true` | 均为 `true` |
| `speculative-config.method` / `num_speculative_tokens` | `dspark` / 5 | `dspark` / 5 |
| `speculative-config.enforce_eager` | `true` | `false` |
| 顶层 `enforce-eager` | `true` | 未显式设置 |
| `compilation-config.cudagraph_mode` | 未显式设置 | `FULL_DECODE_ONLY` |
| `additional-config.enable_cpu_binding` | `true` | `true` |
| `additional-config.multistream_engram_overlap` | `false` | `true` |
| `additional-config.multistream_overlap_shared_expert` | `true` | `true` |
| `additional-config.multistream_dsv4_dsa_overlap` | `true` | `true` |
| `additional-config.combine_quant_mode` | 未显式设置 | 4 |
| `additional-config.scheduler_config.recompute_scheduler_enable` | 未显式设置 | `true` |
| `kv_transfer_config.kv_connector` | `MultiConnector` | `MultiConnector` |
| `kv_transfer_config.kv_role` / `engine_id` | `kv_producer` / `"0"` | `kv_consumer` / `"1"` |
| `kv_transfer_config.kv_load_failure_policy` | `fail` | `fail` |
| `kv_connector_extra_config.connectors[0]` | `MooncakeHybridConnector`，`kv_port="26010"` | `MooncakeHybridConnector`，`kv_port="27010"` |
| `kv_connector_extra_config.connectors[1]` | `AscendStoreConnector`，backend=`memcache` | `AscendStoreConnector`，backend=`memcache` |

最后两行的 `kv_connector_extra_config` 均位于 `kv_transfer_config` 下。
AscendStoreConnector 内的 `lookup_rpc_port` 保持字符串 `"0"`，`use_layerwise` 保持 `false`；
`kv_cache_store_config.local_config_path` 保持 `/usr/local/Ascend/Motor/conf/mmc-local-inprocess.conf`，
并确认运行镜像中存在该配置。`enforce-eager` 和 `speculative-config.enforce_eager` 位于不同层级，
不要互相替换；A5 Decode 的重计算开关在 `additional-config.scheduler_config` 内，不能照抄 A3 的扁平写法。

A5 的完整 `env.json` 还应保留以下公共环境和 P/D 环境。公共环境位于 `motor_common_env`，
引擎环境分别位于 `motor_engine_prefill_env` 与 `motor_engine_decode_env`：

| 环境变量 | 公共或 Prefill 取值 | Decode 取值 |
|----------|---------------------|------------|
| 公共 `CANN_INSTALL_PATH` | `/usr/local/Ascend`，按镜像目录核对 | 使用公共值 |
| 公共 `MOTOR_LOG_ROOT_PATH` | `/root/ascend/log`，按日志目录核对 | 使用公共值 |
| 公共 `MOTOR_ENABLE_IPC_HOST` | 1，生成引擎 Pod 的 `hostIPC: true` | 使用公共值 |
| `VLLM_USE_V2_MODEL_RUNNER` | 1 | 1 |
| `HCCL_OP_EXPANSION_MODE` | `CCU_SCHED` | `CCU_SCHED` |
| `HCCL_BUFFSIZE` | 1024 | 1600 |
| `HCCL_CONNECT_TIMEOUT` | 1200 | 1200 |
| `HCCL_EXEC_TIMEOUT` | 2040 | 2040 |
| `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` | 3000 | 3000 |
| `OMP_PROC_BIND` | 字符串 `"false"` | 字符串 `"false"` |
| `OMP_NUM_THREADS` | 10 | 10 |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` | `expandable_segments:True` |
| `ASCEND_LOCAL_COMM_RES` | JSON 字符串 `"{\"version\":\"1.3\"}"` | 同 Prefill |

需要在原典配基础上合并以下片段，不能用它替换整个文件：

```json
{
  "motor_deploy_config": {
    "deploy_mode": "rbg",
    "hardware_type": "Ascend950",
    "job_id": "motor-dsv41",
    "p_instances_num": 1,
    "d_instances_num": 1,
    "single_p_instance_pod_num": 1,
    "single_d_instance_pod_num": 1,
    "p_pod_npu_num": 8,
    "d_pod_npu_num": 8,
    "image_name": "<实际镜像>",
    "weight_mount_path": "/mnt/weight",
    "rbg_config": {
      "name": "vllm",
      "logical_service_name": "vllm",
      "gang_scheduling": true,
      "scheduler_name": "volcano"
    },
    "prefill_node_selector": {},
    "decode_node_selector": {}
  },
  "motor_engine_prefill_config": {
    "engine_config": {
      "model": "/mnt/weight/DeepSeek-V4.1-Flash"
    }
  },
  "motor_engine_decode_config": {
    "engine_config": {
      "model": "/mnt/weight/DeepSeek-V4.1-Flash"
    }
  }
}
```

- `weight_mount_path` 示例为宿主机 `/mnt/weight`，`set_weight_mount()` 将其挂载到容器内同一路径；
  示例权重目录为 `/mnt/weight/DeepSeek-V4.1-Flash`，必须按实际目录同时修改 P/D 的 `model`。
  权重需在所有候选引擎节点及需要挂载权重的 Coordinator 节点可访问。
- 两个 selector 示例为空，不要求集群预先具有自定义标签；若需 P/D 位于同一指定超节点，
  在两个对象中填写相同的真实成员标签，例如 `example.com/supernode=sp-01`。
  该标签需自行准确维护，不是 Motor 自动生成的标签，也不代替硬件拓扑和调度接入。
- 原 A5 `user_config.json` 仍含 `image_name`、`weight_mount_path` 和模型路径占位符，且没有
  `deploy_mode` 与 `rbg_config`，不能原样提交。
- 片段按字段合并；两个 `engine_config` 仅修改 `model`，不能覆盖原对象并丢失并行度、量化、
  MultiConnector 及其他引擎参数。`kv_cache_store_config` 和完整 `env.json` 也必须保留。
- 必须使用 A5 整套 `env.json`，不要套用 A3 的 `HCCL_OP_EXPANSION_MODE` 等环境变量。
- 本例 `rbg_config` 省略重启策略和协调策略，使用默认值；需要显式配置全部字段时，合并前文
  [完整 rbg_config 示例](#完整-rbg_config-示例)中的对象，并按是否启用 DP 降级调整重启策略。
- 上述资源和环境参数取自 A5 典配；默认 `chip8-node8` 未启用超节点策略，RBG 调度接入仍需按
  [`sp-block` 与超节点调度](#sp-block-与超节点调度)核实，不能将配置合并完成视为调度和推理验收通过。

### A3 DeepSeek-V4.1-Flash 的 MultiConnector 检查

部署 A3 前检查 Prefill 的 `engine_config.kv_transfer_config.kv_connector_extra_config.connectors`。
当前 DeepSeek-V4.1-Flash A3 典配在 `engine_config.kv_transfer_config` 内又嵌套了一层
`kv_transfer_config`，把 `connectors` 放在内层，而
[`_classify_vllm_multi_connector()`](../../../../motor/common/resources/dispatch.py) 只读取
`engine_config.kv_transfer_config.kv_connector_extra_config.connectors`，因此会把 dispatch profile
判定为 `UNKNOWN`。应把 `connectors` 放到 `kv_connector_extra_config` 下，保留原两个 connector 的内容。
A5 的对应层级已正确。这是 `model_configs` 典配自身的问题，不是 RBG 引入的；deployer 的 KV 校验
不会发现该层级错误，不能把“部署 Job 成功”当作 KV 协议正确。

复制 A3 典配后，用下面完整对象替换 Prefill 的 `engine_config.kv_transfer_config`，
不要仅做递归合并而留下旧的内层 `kv_transfer_config`。两个 connector 的角色、端口和 memcache 参数均保留：

```json
{
  "kv_transfer_config": {
    "kv_connector": "MultiConnector",
    "kv_role": "kv_producer",
    "kv_port": "30000",
    "engine_id": "0",
    "kv_connector_extra_config": {
      "connectors": [
        {
          "kv_connector": "MooncakeHybridConnector",
          "kv_role": "kv_producer",
          "kv_port": "30000",
          "kv_connector_extra_config": {}
        },
        {
          "kv_connector": "AscendStoreConnector",
          "kv_role": "kv_producer",
          "kv_connector_extra_config": {
            "backend": "memcache",
            "lookup_rpc_port": "0",
            "use_layerwise": false
          }
        }
      ]
    }
  }
}
```

修正后确认 A3 Prefill 的 `connectors` 路径与 Decode 一致；Decode 保持 `kv_consumer` 和原 `30100`
端口，不能将 Prefill 对象整体复制给 Decode。本文只修正部署说明和替换示例，原始 `model_configs`
文件仍需在用户复制的配置目录内按上述结构修正。

## 协调策略

首次部署省略 `rbg_config.coordinated_policy` 时，生成与 RBG 同名、同 namespace 的 CoordinatedPolicy。
默认规则名为 `engine-scaling`，覆盖当前业务形态的引擎角色，使用
`maxSkew: "100%"` 和 `progression: OrderReady`。

自定义时，在已有 `motor_deploy_config.rbg_config` 中添加以下字段；这是 PD 分离形态的示例：

```json
{
  "coordinated_policy": {
    "policies": [
      {
        "name": "engine-scaling",
        "roles": ["prefill", "decode"],
        "strategy": {
          "scaling": {
            "maxSkew": "100%",
            "progression": "OrderReady"
          }
        }
      }
    ]
  }
}
```

以上片段加在 `motor_deploy_config.rbg_config` 内。各字段说明如下：

| JSON 路径 | 说明 | 怎么改 |
|-----------|------|--------|
| `coordinated_policy` | 跨角色协调策略配置；省略时生成默认的 `engine-scaling` | 需要自定义规则时添加；不写通常保留默认或线上策略 |
| `coordinated_policy.policies` | 策略规则数组，可包含多条规则 | 按业务形态增减规则，每条规则至少包含 `name`、`roles`、`strategy` |
| `coordinated_policy.policies[].name` | 规则名称 | 默认 `engine-scaling`；自定义时保持名称唯一，便于识别 |
| `coordinated_policy.policies[].roles` | 参与协调扩缩容的角色列表 | 按实际启用的引擎角色填写；PD 分离通常为 `["prefill", "decode"]` |
| `coordinated_policy.policies[].strategy` | 该规则的策略定义 | 通常保持默认的 `scaling` 结构 |
| `coordinated_policy.policies[].strategy.scaling` | 扩缩容协调参数 | 不需要调整时保持此层不变，只改下面的两个字段 |
| `coordinated_policy.policies[].strategy.scaling.maxSkew` | 角色间副本差异上限 | 启用组调度时必须为 `"100%"`，不要改小 |
| `coordinated_policy.policies[].strategy.scaling.progression` | 扩缩容推进方式 | 默认 `OrderReady`；多 Pod 实例不要使用 `OrderScheduled` |
| `coordinated_policy.policies[].strategy.rollingUpdate` | 可选的跨角色滚动更新协调对象 | 完整示例已展示；不需要时删除整个对象，默认协调策略不生成此项 |
| `coordinated_policy.policies[].strategy.rollingUpdate.maxSkew` | 角色间更新进度的最大差异 | 可填正整数或百分比字符串，示例为整数 `1`；区别于 Gang 下必须为 `"100%"` 的 `scaling.maxSkew` |
| `coordinated_policy.policies[].strategy.rollingUpdate.partition` | 滚动更新分区，低于分区的实例保持原版本 | 可填非负整数或百分比字符串，示例为整数 `0`，表示不保留分区 |
| `coordinated_policy.policies[].strategy.rollingUpdate.maxUnavailable` | 滚动更新允许的不可用实例数 | 可填正整数或百分比字符串，示例为整数 `1`；按实际可用性要求调整 |

通常只需要改 `roles`；`maxSkew` 和 `progression` 保持默认即可。修改后通过正常部署入口重新提交配置以更新策略。
`rollingUpdate` 的整数填写 JSON 数字，百分比填写字符串；`scaling.maxSkew` 在当前 Motor 校验中
必须填写百分比字符串。`coordinated_policy: {}` 和 `policies: []` 均不能用作关闭协调策略的开关。

后续省略该字段通常保留线上策略；业务形态切换时，
生成器会刷新符合默认定义的 `engine-scaling` 规则的角色列表。启用组调度时 `maxSkew` 必须为
`"100%"`；多 Pod 实例不支持 `OrderScheduled`。
`--update_scaling_policy` 专门更新 HPA，不修改 CoordinatedPolicy。

## 常用命令

以下命令在 `examples/deployer/` 下执行。`CONFIG_DIR` 指向包含已配置好的 `user_config.json`
和 `env.json` 的目录；示例 namespace `mindie` 需与配置中的 `job_id` 一致：

```bash
CONFIG_DIR=/path/to/rbg-config
NAMESPACE=mindie

# 预览本次资源，仍需连接集群执行查询和前置检查
python3 deploy.py --config_dir "$CONFIG_DIR" --dry-run

# 部署或重新提交配置
python3 deploy.py --config_dir "$CONFIG_DIR"

# 仅更新白名单内的运行配置，不主动重启 Pod
python3 deploy.py --config_dir "$CONFIG_DIR" --update_config

# 手动扩缩容：只修改实例数，目标角色须已解除 HPA/Adapter 管理
python3 deploy.py --config_dir "$CONFIG_DIR" --update_instance_num

# 热更新已存在的 P/D HPA，policy.json 格式见下一节
python3 deploy.py --update_scaling_policy policy.json

# 清理 RBG：命令行必须带 MOTOR_DEPLOY_MODE=rbg；也可使用 TUI 删除
MOTOR_DEPLOY_MODE=rbg bash ../cloud_native_deploy/cleanup.sh "$NAMESPACE"
```

部署、预览、更新运行配置和手动扩缩容默认执行安全预检。生产环境须配置推理 TLS 与管理面 API Key，
具体字段见 [Deployer 安全预检](../../README.md#安全预检)，并移除 Helm values / Job 参数中的
`--skip-check`。该参数仅限隔离的本地或测试环境验证，生产环境不得使用。

`--update_config` 只将[白名单](../update_config_whitelist.py)内的变更合入线上基线，非白名单变更会被
忽略并记录警告。它仅修改 `motor-config` 中的 `user_config.json`，不修改 `env.json`、启动脚本或
Pod 模板。支持热加载的运行配置经容器配置同步和进程监听后生效，无需等待下一次部署。

部署与预览产物位于 `output_yamls/<namespace>/<rbg_name>/`：

| 文件 | 内容 |
|------|------|
| `prerequisites.yaml` | 运行时 SA/RBAC、`motor-config` 及按需创建的 PVC |
| `services.yaml` | 业务 Service |
| `policy.yaml` | 本次提交的 CoordinatedPolicy；保留线上策略时可为空 |
| `rbg.yaml` | RoleBasedGroup |
| `hpas.yaml` | 本次提交的 HPA；未启用或保留线上 HPA 时可为空 |

## 扩缩容

手动扩缩容只允许修改已有的实例数字段，不改变每实例 Pod 数、镜像或其他配置。
目标角色启用了 `scalingAdapter`、仍存在对应 Adapter 或 HPA 时，手动扩缩容会被拒绝。

启用 HPA 前，先完成 Coordinator 指标采集与 External Metrics Adaptor 对接，见
[K8s 指标采集示例](../../../features/observability/k8s/README.md)。在已有 `user_config.json`
顶层添加以下示例，然后通过正常部署入口重新提交：

```json
{
  "scaling_policy": {
    "prefill": {
      "min_replicas": 1,
      "max_replicas": 4,
      "metric": "motor_prefill_utilization",
      "target": 0.8
    },
    "decode": {
      "min_replicas": 1,
      "max_replicas": 4,
      "metric": "motor_decode_utilization",
      "target": 0.8
    }
  }
}
```

以上片段加在 `user_config.json` 顶层（与 `motor_deploy_config` 同级）。各字段说明如下：

| JSON 路径 | 说明 | 怎么改 |
|-----------|------|--------|
| `scaling_policy` | HPA 扩缩容配置；省略时通常保留线上 HPA | 启用或调整 HPA 时添加；显式写 `{}` 表示关闭所有受管 HPA |
| `scaling_policy.prefill` | Prefill 角色的 HPA 配置对象 | 不需要 Prefill HPA 时删除整个 `prefill` 对象 |
| `scaling_policy.prefill.min_replicas` | Prefill HPA 最小实例数 | 按业务最低副本填写，通常大于等于 1，且不能大于 `max_replicas` |
| `scaling_policy.prefill.max_replicas` | Prefill HPA 最大实例数 | 按容量上限填写，必须大于等于 `min_replicas` |
| `scaling_policy.prefill.metric` | Prefill 扩缩容使用的指标名 | 默认 `motor_prefill_utilization`；改成 Coordinator/External Metrics Adaptor 实际暴露的指标名 |
| `scaling_policy.prefill.target` | Prefill 目标利用率 | `0.8` 表示 80%；按实际水位调整 |
| `scaling_policy.decode` | Decode 角色的 HPA 配置对象 | 不需要 Decode HPA 时删除整个 `decode` 对象 |
| `scaling_policy.decode.min_replicas` | Decode HPA 最小实例数 | 按业务最低副本填写，通常大于等于 1，且不能大于 `max_replicas` |
| `scaling_policy.decode.max_replicas` | Decode HPA 最大实例数 | 按容量上限填写，必须大于等于 `min_replicas` |
| `scaling_policy.decode.metric` | Decode 扩缩容使用的指标名 | 默认 `motor_decode_utilization`；必须与实际暴露的指标名一致 |
| `scaling_policy.decode.target` | Decode 目标利用率 | `0.8` 表示 80%；按实际水位调整 |

`metric` 名称必须与 Coordinator 指标采集及 External Metrics Adaptor 暴露的名称一致。修改后通过正常部署入口重新提交；`--update_config` 不会更新 HPA。

目标角色需具有正数实例。部署流程启用角色的 ScalingAdapter，等待其绑定且 `scale` 子资源初始化后，
创建 `{rbg_name}-{role}-hpa`。HPA 的 `scaleTargetRef` 指向
`RoleBasedGroupScalingAdapter/{rbg_name}-{role}`，调整完整实例数量，不改变每实例 Pod 数。
External 指标 selector 包含 `kubernetes_namespace={job_id}` 和
`infer_huawei_com_inferservice_name={logical_service_name}-0`。

| 重新提交配置时的意图 | 顶层 `scaling_policy` | 行为 |
|--------------------|----------------------|------|
| 启用或调整 HPA | 指定角色的非空对象 | 生成并提交这些角色的 HPA，移除不再列出的受管 HPA |
| 保留线上 HPA | 省略字段 | 通常保留；角色停用为 0 副本时移除其 HPA |
| 关闭所有受管 HPA | 显式 `{}` | 删除受管 HPA，关闭并等待移除对应 Adapter |

上述操作使用正常部署入口，不能通过 `--update_config` 完成。关闭 HPA 并确认 Adapter 已移除后，
才可用 `--update_instance_num` 手动扩缩容；正常部署重新提交时，未受 Adapter 管理的角色采用配置中的实例数。

热更新已有 P/D HPA 使用 `--update_scaling_policy policy.json`。例如 namespace 为 `mindie` 时：

```json
{
  "namespace": "mindie",
  "roles": {
    "prefill": {
      "spec": {
        "maxReplicas": 6
      }
    }
  }
}
```

以上内容作为 `--update_scaling_policy policy.json` 的 `policy.json` 文件。各字段说明如下：

| JSON 路径 | 说明 | 怎么改 |
|-----------|------|--------|
| `namespace` | 目标 namespace | 必须与 `motor_deploy_config.job_id` 一致；该 namespace 中只能有一套受管 RBG |
| `roles` | 要更新的角色 HPA 集合 | 当前入口只支持 `prefill` 和 `decode`，可同时写两个角色 |
| `roles.prefill` | Prefill 角色的 HPA 更新片段 | 只写需要修改的字段；该角色没有 HPA 时先通过正常部署创建 |
| `roles.prefill.spec` | HPA 的 `spec` 片段，不是完整 HPA 对象 | 只写 `spec` 下的字段，不要写 `metadata` 或 `status` |
| `roles.prefill.spec.maxReplicas` | 修改后的最大实例数 | 按容量上限调整，必须大于等于当前 `minReplicas` |

如需同时更新 Decode，按 `roles.decode.spec` 的相同结构添加。此入口要求 namespace 中恰好有一个受管 RBG，
且指定角色的 HPA 已存在；不创建缺失 HPA，也不修改 `scaleTargetRef`。对象按字段合并、数组整体替换。
修改成功后若希望后续部署继续使用新策略，需同步维护原部署配置中的 `scaling_policy`；
该更新命令不回写原配置。

## 配置更新与清理

镜像、环境变量和支持更新的 Pod 模板字段通过正常部署入口重新提交，相关角色可能发生滚动更新。
组调度、调度器、`scheduling_queue`，以及活动引擎角色的实例形态、每实例 Pod 数和重启策略变更，
会被原部署的更新检查拒绝；应先清理，再按新配置部署。切换 `deploy_mode` 也应清理原形态后重新部署。
业务 Service 的端口新增、改名和不支持的类型转换需重建；省略已有端口表示保留，不表示删除端口。

TUI 删除与 `cleanup.sh` 使用同一 RBG 清理流程：先处理受管 HPA，再删除 RBG、等待其 Pod、
RoleInstance、RoleInstanceSet 和 Adapter 退出，随后清理业务 Service、协调策略、运行时 SA/RBAC 和
`motor-config`。保留数据 PVC 和外部资源。命令行调用 `cleanup.sh` 时必须设置
`MOTOR_DEPLOY_MODE=rbg`，否则会走旧的非 RBG 清理流程。旧 `examples/deployer/delete.sh` 按本地 YAML 删除资源，
不具有上述清理顺序与数据保留语义。

## 支持范围

- 同一 namespace 只部署一套 RBG，运行时 ConfigMap 固定为 `motor-config`。
- RBG 的实例联动重建由 `restartPolicy` 实现。`ras_monitor` 仍通过旧 `delete.sh` 删除并重新部署整套服务，
  不具备 RBG 专用清理流程的数据 PVC 保留语义，不推荐作为 RBG 的实例恢复入口；`kubectl delete pod`
  只补建单个 Pod，也不能替代整实例重建。详见
  [ras_monitor 说明](../../../features/fault_tolerance/ras_monitor/readme.md)。
- 不支持 P2D；显式设置 `enable_scale_p2d=true` 会报错，省略或关闭时落实为 `false`。
- 不支持容器快照；设置 `motor_container_snapshot_config.enable_snapshot=true` 会报错。
- 正常部署 Job 完成表示资源已提交，不代表所有工作负载就绪或业务推理验证完成。

## 部署后验收

1. 生成的 RBG 中 P/D 的 `replicas`、每实例 Pod 数、NPU `requests`/`limits` 与所选典配一致。
2. P/D 的 `schedulerName`、硬件 selector、自定义超节点 selector 已实际出现在 Pod 上。
3. 启用 Gang 时 PodGroup 已创建，Pod 引用同一个预期组，组成员数量与模板一致。若依赖 Ascend 硬件策略，
   还需确认组级 NPU 识别、实际读取的硬件注解及调度器选中的处理器；不能只检查 P/D 模板注解。
4. 配置了目标超节点约束时，P/D Pod 的 `spec.nodeName` 对应节点都属于该超节点；不能只检查 PodGroup 名称。
5. NodeManager 完成注册、引擎成功启动、KV 通道正常，实际推理请求成功；Pod 处于 Running 不能替代这一项。
6. 限定目标超节点时，扩容仍需有足够资源；selector 范围内资源不足时，新 Pod 会等待调度，
   不会自动放宽到其他超节点。
