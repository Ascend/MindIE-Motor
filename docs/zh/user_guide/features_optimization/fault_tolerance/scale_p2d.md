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

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 说明 |
|----------|------|
| 部署场景 | 该特性只支持PD分离服务部署。|
| 软件依赖 | 该特性依赖MindCluster的优先级调度与实例强制删除能力，且版本为26.1.0及以上。 |

## 特性使用

### 使用场景

| 维度 | 说明 |
|------|------|
| 部署形态 | PD分离服务部署 |
| 故障对象 | Decode实例（role == decode） |
| 故障级别 | 实例级故障达到L4、L5或L6 |
| 节点故障 | D实例上存在L3及以上设备级硬件故障的节点，或节点元数据缺失 |
| 前置隔离 | D实例已脱离`initial` / `active`等业务活跃态（由FaultManager触发隔离后进入`inactive`等状态） |

满足以下全部条件时，FaultManager会异步触发ScaleP2D恢复流程：

- enable_scale_p2d == true
- 故障实例的role == "decode"
- 实例故障级别为L4 / L5 / L6（**需要补充实例故障级别解释**）

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

启用ScaleP2D需同时完成Controller侧JSON配置（user_config.json配置文件中的motor_controller_config字段）与InferServiceSet YAML配置（CRD部署场景）。

**操作步骤**

1. 配置user_config.json配置文件中的motor_controller_config字段，示例如下，更多参数解释请参见[user_config.json配置文件全量参数说明](../../configuration/config_reference.md#motor_controller_config)。

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
   | `enable_fault_tolerance` | bool | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 取值为`true`才启动FaultManager |
   | `enable_scale_p2d` | bool | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | `false` | 是否启用ScaleP2D（用户侧默认`false`） |
   | `scale_p2d_d_instance_reinit_wait_timeout` | int | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | `60` | ScaleP2D执行抢占前，等待D实例自恢复（重初始化）的最长时间（秒）。等待期间若D实例恢复为`initial` / `active`，则不再执行ScaleP2D；超时后若D实例仍处于`inactive`等可抢占状态，则继续后续P实例选择流程。 |
   | `strategy_center_check_interval` | int | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 策略中心轮询间隔（秒） |

2. 开启InferServiceSet CRD侧的优先级调度能力。

   修改文件：examples/deployer/yaml_template/infer_service_template.yaml（CRD模式下deploy脚本据此生成output_yamls/infer_service.yaml）。

   在`InferServiceSet.spec.template`下增加`schedulingStrategy`，类型设为`Priority`，示例如下：

   ```yaml
   spec:
     template:
       schedulingStrategy:
         type: Priority
       roles:
         # ...
   ```

3. 为prefill / decode角色配置priority字段。

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
    | priority | int | 1–32 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 数值越小，调度优先级越高 |

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
    | fault-scheduling | string | grace/external-force | 是 | grace | 取值为external-force，表示开启实例级重调度；强制删除原实例并级联删除Pod，供ScaleP2D实现P实例的强制释放。 |

### 验证特性

**内容缺失，需要人工补齐。**

## 常见问题

**依据原文上下文内容重组，请进行人工校验。**

### instance_not_in_instance_manager

**问题描述**：出现`instance_not_in_instance_manager`关键词。

**原因分析**：D实例不存在。

**解决步骤**：检查etcd / InstanceManager同步。

### ScaleP2D not needed + initial/active

**问题描述**：出现`ScaleP2D not needed` + `initial/active`关键词。

**原因分析**：D未隔离。

**解决步骤**：检查`separate_instance`流程。

### did not become INACTIVE

**问题描述**：出现`did not become INACTIVE`关键词。

**原因分析**：状态检查超时。

**解决步骤**：检查隔离与状态上报延迟。

### Node metadata missing

**问题描述**：出现`Node metadata missing`关键词。

**原因分析**：节点未同步。

**解决步骤**：检查ResourceMonitor / pod_ip映射。

### no_p_instances

**问题描述**：出现`no_p_instances`关键词。

**原因分析**：无P实例。

**解决步骤**：检查部署与注册。

### Insufficient Prefill nodes

**问题描述**：出现`Insufficient Prefill nodes`关键词。

**原因分析**：P容量不足。

**解决步骤**：扩容P或降低故障节点数。

### Failed to stop P instance node

**问题描述**：出现`Failed to stop P instance node`关键词。

**原因分析**：NodeManager不可达。

**解决步骤**：检查进程、网络、Pod生命周期。

### algorithm_not_implemented

**问题描述**：出现`algorithm_not_implemented`关键词。

**原因分析**：选择算法未实现。

**解决步骤**：联系开发确认P实例选择策略。
