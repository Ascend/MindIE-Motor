# ScaleP2D故障恢复

## 特性介绍

ScaleP2D（Scale Prefill to Decode）是MindIE Motor在PD分离场景下的一种故障自愈策略。当D实例因L4–L6级硬件故障导致部分节点不可用时，系统会主动停止若干P实例，释放算力与节点资源，为故障D实例的恢复或替换腾出容量。

### 工作原理

ScaleP2D恢复大致分为四步：

| 步骤 | 说明 |
|------|------|
| 1. 加载D实例 | 统计D实例上L3+故障节点数（缺失元数据视同故障），计算需腾出的节点数`num_required_node`。 |
| 2. 等待D自恢复 | 在`scale_p2d_d_instance_reinit_wait_timeout`内轮询D实例状态；若恢复为`initial` / `active`则取消ScaleP2D；超时后若仍为`inactive`等可抢占状态则继续。 |
| 3. 选择P实例 | 在可用P容量内选取待停止的P实例（可用节点 = `nodes_per_P × (P_count - 1)`）。 |
| 4. 停止P实例 | 对选中P实例的所有NodeManager下发`stop`，由CRD强制回收Pod并释放节点。 |

### 约束与限制

| 约束维度 | 说明 |
|----------|------|
| 部署场景 | <ul><li>仅支持PD分离服务部署，且故障对象必须是Decode实例。</li><li>不适用于Prefill实例故障、故障级别未达到L4的场景。</li><li>不适用于Ascend950 PR + DT异构组网：停掉P后释放的是PR节点，D无法调度到这些节点上恢复。建议该组网下将`enable_scale_p2d`设为`false`。详见[PD分离服务部署](../../deployment/k8s/pd_disaggregation_deployment.md)中「PD异构（PR / DT）调度」。</li></ul> |
| 软件依赖 | 依赖MindCluster的优先级调度与实例强制删除能力，MindCluster版本须为26.1.0及以上。 |
| 其他限制 | <ul><li>须至少保留1个P实例；可用节点不足时恢复失败。</li><li>默认各P实例节点数相同。</li><li>当前P实例选择按实例状态（优先`initial`）和实例ID排序，后续可能接入负载/优先级模型。</li><li>在Atlas 800I A2上，隔离类故障码（如`CardNetworkUnhealthy`）对Prefill / Decode走NmSuicide整实例停止，不走ScaleP2D。</li></ul> |

## 特性使用

### 环境准备

- 已参见[PD分离服务部署](../../deployment/k8s/pd_disaggregation_deployment.md)完成环境部署。ScaleP2D仅支持PD分离，不适用于PD混部。
- MindCluster版本须为26.1.0及以上，已安装Infer Operator，并具备优先级调度与实例强制删除能力。
- 使用InferServiceSet CRD部署模式。
- 具备K8s集群管理员的kubectl权限。
- 建议Prefill实例数不少于2：ScaleP2D须至少保留1个P实例；1P1D场景可用节点为0，无法完成抢占。

### 使用场景

| 维度 | 说明 |
|------|------|
| 部署形态 | PD分离服务部署 |
| 故障对象 | Decode实例（role == decode） |
| 故障级别 | 实例级故障达到L4、L5或L6 |
| 节点故障 | D实例上存在L3及以上设备级硬件故障的节点，或节点元数据缺失 |
| 前置隔离 | D实例已脱离`initial` / `active`等业务活跃态（由FaultManager触发隔离后进入`inactive`等状态） |

满足以下全部条件时，FaultManager会异步触发ScaleP2D恢复流程：

- `enable_fault_tolerance == true` 且 `enable_scale_p2d == true`
- 故障实例的`role == "decode"`
- 实例故障级别为L4 / L5 / L6

实例故障级别由FaultManager按该实例所在节点的最高硬件/软件故障映射，含义如下：

| 等级 | 原始故障类型 | 含义 | ScaleP2D行为 |
|------|-------------|------|--------------|
| L4 | FreeRestartNPU | 需隔离NPU | 触发缩P保D |
| L5 | RestartNPU | 需重启NPU | 委托L4策略，行为与L4相同 |
| L6 | SeparateNPU / PreSeparateNPU / ManuallySeparateNPU | 需分离NPU或节点重启 | 委托L4策略，行为与L4相同 |

L3及以下不触发ScaleP2D：L3需人工介入，L2走Token重推等其他策略，L1仅通知。

### 使用样例

启用ScaleP2D需同时完成Controller侧JSON配置（`user_config.json`中的`motor_controller_config`字段）与InferServiceSet YAML配置（CRD部署场景）。

**操作步骤**

1. 配置`user_config.json`中的`motor_controller_config`字段，示例如下，更多参数解释请参见[user_config.json配置文件全量参数说明](../../configuration/config_reference.md#motor_controller_config)。

   ```json
   {
     "fault_tolerance_config": {
       "enable_fault_tolerance": true,
       "enable_scale_p2d": true,
       "scale_p2d_d_instance_reinit_wait_timeout": 60,
       "strategy_center_check_interval": 1
     }
   }
   ```

   **表 1** 参数说明

   | 配置项 | 类型 | 取值范围 | 是否必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | `enable_fault_tolerance` | bool | true / false | 否 | true | 是否启用Motor Controller故障自愈。取值为`true`才启动FaultManager；关闭后ScaleP2D不会实际生效。 |
   | `enable_scale_p2d` | bool | true / false | 否 | true | 是否启用ScaleP2D。需同时`enable_fault_tolerance=true`才实际生效。建议在用户配置中显式设为`true`。 |
   | `scale_p2d_d_instance_reinit_wait_timeout` | int | 1–600 | 否 | 60 | ScaleP2D执行抢占前，等待D实例自恢复（重初始化）的最长时间（秒）。等待期间若D实例恢复为`initial` / `active`，则不再执行ScaleP2D；超时后若D实例仍处于`inactive`等可抢占状态，则继续后续P实例选择流程。 |
   | `strategy_center_check_interval` | int | > 0 | 否 | 10 | 策略中心轮询间隔（秒）。策略中心会由故障上报即时唤醒，该间隔仅作为兜底周期重评估。示例取`1`可加快巡检。 |

2. 开启InferServiceSet CRD侧的优先级调度能力。

   修改文件：`examples/deployer/yaml_template/infer_service_template.yaml`（CRD模式下deploy脚本据此生成`output_yamls/infer_service.yaml`）。

   在`InferServiceSet.spec.template`下增加`schedulingStrategy`，类型设为`Priority`，示例如下：

   ```yaml
   spec:
     template:
       schedulingStrategy:
         type: Priority
       roles:
         # ...
   ```

3. 为prefill / decode角色配置`priority`字段。

   在`prefill`、`decode`两个role的`spec`同级增加`priority`字段（仅开启优先级调度时生效）。

   PD分离场景下，建议prefill的`priority`数值大于decode（即prefill优先级最低，更易被抢占），与ScaleP2D优先释放P算力的策略一致。示例如下：

   ```yaml
       - name: prefill
         replicas: 4
         priority: 2          # 优先级最低
         # ...

       - name: decode
         replicas: 4
         priority: 1
         # ...
   ```

   | 配置项 | 类型 | 取值范围 | 是否必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | priority | int | 1–32 | 否 | 无 | 仅`schedulingStrategy.type`为`Priority`时生效。数值越小，调度优先级越高。未配置时该角色不参与优先级抢占。 |

4. 开启InferServiceSet CRD侧的实例强制删除能力。

   将`prefill`、`decode`角色Pod模板（`spec.template.metadata.labels`）中的`fault-scheduling`由默认的`grace`改为`external-force`：

   ```yaml
           template:
             metadata:
               labels:
                 fault-scheduling: external-force   # 原为 grace
                 fault-retry-times: "10000"
                 app: mindie-server
                 # ...
   ```

   | 配置项 | 类型 | 取值范围 | 是否必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | fault-scheduling | string | grace / external-force | 是 | grace | 取值为`external-force`时开启实例级重调度：强制删除原实例并级联删除Pod，供ScaleP2D实现P实例的强制释放。 |

5. 创建与`job_id`同名的命名空间后，使用deploy脚本部署服务。`{namespace}`须与`user_config.json`中的`job_id`一致。

   ```bash
   kubectl create namespace {namespace}
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm --nostep
   ```

   部署成功时日志出现`all deploy end.`和`Deploy complete.`。

### 验证特性

以下以PD分离1P1D为例。命令中的`{namespace}`须替换为实际命名空间。

1. 检查Pod是否全部Running。

   ```bash
   kubectl get pod -n {namespace}
   ```

   回显示例如下：

   ```text
   NAME                         READY   STATUS    RESTARTS   AGE
   vllm-0-controller-0-xxx      1/1     Running   0          1m
   vllm-0-coordinator-0-xxx     1/1     Running   0          1m
   vllm-0-decode-0-0            1/1     Running   0          1m
   vllm-0-kv-store-0-xxx        1/1     Running   0          1m
   vllm-0-prefill-0-0           1/1     Running   0          1m
   ```

   Prefill / Decode Pod的`fault-scheduling`标签应为`external-force`：

   ```bash
   kubectl get pod -n {namespace} -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.fault-scheduling}{"\n"}{end}'
   ```

   ```text
   vllm-0-decode-0-0     external-force
   vllm-0-prefill-0-0    external-force
   ```

2. 检查InferServiceSet是否已开启优先级调度，且prefill / decode的`priority`已生效。

   ```bash
   kubectl get inferserviceset vllm -n {namespace} -o jsonpath='{.spec.template.schedulingStrategy}{"\n"}{range .spec.template.roles[*]}{.name} priority={.priority} replicas={.replicas}{"\n"}{end}'
   ```

   回显示例如下：

   ```text
   {"type":"Priority"}
   controller priority= replicas=1
   coordinator priority= replicas=1
   prefill priority=2 replicas=1
   decode priority=1 replicas=1
   ```

3. 检查ConfigMap中的ScaleP2D配置是否已写入。

   ```bash
   kubectl get configmap motor-config -n {namespace} -o jsonpath='{.data.user_config\.json}' | python3 -m json.tool | grep -A 6 fault_tolerance_config
   ```

   回显示例如下：

   ```json
   "fault_tolerance_config": {
     "enable_fault_tolerance": true,
     "enable_scale_p2d": true,
     "scale_p2d_d_instance_reinit_wait_timeout": 60,
     "strategy_center_check_interval": 1
   }
   ```

4. 检查Controller日志，确认FaultManager与ScaleP2D已启用。

   ```bash
   kubectl logs -n {namespace} -l app=mindie-motor-controller | grep -E "Scale P2D|FaultManager|strategy center"
   ```

   特性生效时出现以下关键字：

   ```text
   FaultManager initialized.
   Fault tolerance strategy center started
   FaultManager started.
     ├─ Advanced RAS:         Enabled
     │   ├─ Scale P2D:        Enabled
   ```

   P / D实例注册后，FaultManager会为对应节点拉起ResourceMonitor，例如：

   ```text
   FaultManager update instance {job_id}-vllm-0-d0 with event: ObserverEvent.INSTANCE_INITIAL.
   Added instance 1 ({job_id}-vllm-0-d0) with 1 nodes
   Resource monitor started for node {decode-node}
   FaultManager update instance {job_id}-vllm-0-p0 with event: ObserverEvent.INSTANCE_INITIAL.
   Added instance 2 ({job_id}-vllm-0-p0) with 1 nodes
   Resource monitor started for node {prefill-node}
   ```

5. 发生真实L4–L6 Decode故障后，在Controller日志中查找ScaleP2D恢复关键字。

   日志前缀：`[motor/controller/fault_tolerance/scale_p2d]`或`[fault_tolerance][scale_p2d.py]`

   | 日志关键词 | 含义 |
   |------------|------|
   | ScaleP2D strategy started | 已触发ScaleP2D恢复 |
   | D instance ready for ScaleP2D | D实例已隔离，进入P实例选择 |
   | P instances selected for kill | 已选出待停止的P实例 |
   | ScaleP2D recovery succeeded | 恢复成功 |
   | ScaleP2D recovery failed | 恢复失败，结合`last_error`排查 |

   >[!NOTE] 说明
   >可用P节点计算公式为`nodes_per_P × (P_count - 1)`，必须至少保留1个P实例。1P1D部署下可用节点为0，即使D实例故障也不会完成抢占，日志会出现`Insufficient Prefill nodes`。验证完整恢复流程时，P实例数至少为2。

## 常见问题

### instance_not_in_instance_manager

**问题描述**：Controller日志出现`instance_not_in_instance_manager`或`instance_not_found`。

**原因分析**：触发ScaleP2D时，InstanceManager中已找不到对应D实例，常见于ETCD同步未完成或实例已被删除。

**解决步骤**：

1. 检查Controller与ETCD连通性，确认实例注册已完成。
2. 执行`kubectl get pod -n {namespace}`，确认Decode实例Pod仍存在。
3. 查看Controller日志中InstanceManager的注册/删除记录。

### ScaleP2D not needed + initial/active

**问题描述**：日志出现`D instance recovered, ScaleP2D not needed`，状态为`initial`或`active`。

**原因分析**：等待窗口内D实例已自恢复，或FaultManager尚未将D实例隔离为`inactive`，ScaleP2D主动取消抢占。

**解决步骤**：

1. 确认该D实例的故障级别是否已达到L4及以上。
2. 检查`separate_instance`隔离流程是否把D实例置为`inactive`。
3. 若D已恢复业务，无需再执行ScaleP2D。

### did not become INACTIVE

**问题描述**：日志出现`did not become INACTIVE`或`D instance status check timed out`。

**原因分析**：在`scale_p2d_d_instance_reinit_wait_timeout`内，D实例一直未进入可抢占状态。

**解决步骤**：

1. 检查隔离与状态上报是否延迟。
2. 适当增大`scale_p2d_d_instance_reinit_wait_timeout`（取值范围1–600）。
3. 核对InstanceManager中该实例的`status`是否与节点故障同步。

### Node metadata missing

**问题描述**：日志出现`FaultManager returned no node data`，或将全部D节点计为故障节点。

**原因分析**：FaultManager尚未同步到该实例的节点元数据，ResourceMonitor / pod_ip映射未建立。

**解决步骤**：

1. 确认FaultManager已启动，且目标节点的`mindx-dl-deviceinfo-*` ConfigMap存在。
2. 检查ResourceMonitor是否已监听对应节点。
3. 核对NodeManager的`pod_ip`与FaultManager节点表是否一致。

### no_p_instances

**问题描述**：日志出现`no_operational_p_instances`或`No operational Prefill instances`。

**原因分析**：集群中没有处于`initial` / `active`的P实例，无法释放节点。

**解决步骤**：

1. 检查Prefill部署与注册是否成功。
2. 确认P实例未被其他故障策略停止。

### Insufficient Prefill nodes

**问题描述**：日志出现`Insufficient Prefill nodes for ScaleP2D`。

**原因分析**：可用P节点数小于`num_required_node`。可用节点 = `nodes_per_P × (P_count - 1)`。

**解决步骤**：

1. 扩容P实例，保证故障后仍至少保留1个P实例。
2. 或降低单D实例上的L3+故障节点数后再观察。

### Failed to stop P instance node

**问题描述**：日志出现`Failed to stop P instance node`。

**原因分析**：向P实例NodeManager下发`stop`失败，进程、网络或Pod生命周期异常。

**解决步骤**：

1. 检查目标P实例Pod是否Ready，NodeManager端口是否可达。
2. 确认`fault-scheduling`已设为`external-force`，CRD能够强制回收Pod。
3. 查看P实例与Controller之间的网络连通性。

### algorithm_not_implemented

**问题描述**：日志出现`Using placeholder P instance selection algorithm`，`reason=algorithm_not_implemented`。

**原因分析**：当前P实例选择为按状态和实例ID排序的占位实现。该WARNING表示尚未接入负载/优先级代价模型，不代表本次ScaleP2D失败。

**解决步骤**：无需处理。若需按负载选择P实例，联系开发确认后续选择策略。
