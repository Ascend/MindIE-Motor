# ScaleP2D 故障恢复

## 特性介绍

**ScaleP2D**（Scale Prefill to Decode）是 MindIE Motor 在 **PD 分离**（Prefill / Decode 解耦）场景下的一种故障自愈策略。当 **Decode（D）实例** 因 **L4–L6 级硬件故障** 导致部分节点不可用时，系统会 **主动停止若干 Prefill（P）实例**，释放算力与节点资源，为故障 D 实例的恢复或替换腾出容量。

## 版本说明

本特性依赖 MindCluster 的优先级调度与实例强制删除能力，**需要 MindCluster 版本为 26.1.0 及以上**才支持。

## 适用场景

| 维度 | 说明 |
|------|------|
| 部署形态 | PD 分离：P 实例负责 Prefill，D 实例负责 Decode |
| 故障对象 | **Decode 实例**（`role == decode`） |
| 故障级别 | 实例级故障达到 **L4、L5 或 L6** |
| 节点故障 | D 实例上存在 **L3 及以上** 设备级硬件故障的节点，或节点元数据缺失 |
| 前置隔离 | D 实例已脱离 `initial` / `active` 等业务活跃态（由 FaultManager 触发隔离后进入 `inactive` 等状态） |

**不适用于：**

- Prefill 实例故障
- 故障级别 ≤ L3 且未升级到 L4+
- `enable_scale_p2d == false`

## 触发条件

满足以下全部条件时，FaultManager 会异步触发 ScaleP2D 恢复流程：

1. `enable_scale_p2d == true`
2. 故障实例的 `role == "decode"`
3. 实例故障级别为 **L4 / L5 / L6**

## 恢复流程说明

ScaleP2D 恢复大致分为四步：

| 步骤 | 说明 |
|------|------|
| 1. 加载 D 实例 | 统计 D 实例上 L3+ 故障节点数（缺失元数据视同故障），计算需腾出的节点数 `num_required_node` |
| 2. 等待 D 自恢复 | 在 `scale_p2d_d_instance_reinit_wait_timeout` 内轮询 D 实例状态；若恢复为 `initial` / `active` 则取消 ScaleP2D；超时后若仍为 `inactive` 等可抢占状态则继续 |
| 3. 选择 P 实例 | 在可用 P 容量内选取待停止的 P 实例（可用节点 = `nodes_per_P × (P_count - 1)`） |
| 4. 停止 P 实例 | 对选中 P 实例的所有 NodeManager 下发 `stop`，由 CRD 强制回收 Pod 并释放节点 |

更完整的设计说明见 [ScaleP2D 设计文档](../../design/fault_tolerance/scale_p2d.md)。

## 配置说明

启用 ScaleP2D 需同时完成 **Controller 侧 JSON 配置**与 **InferServiceSet YAML 配置**（CRD 部署场景）。

### Controller 配置

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `enable_fault_tolerance` | bool | 须 `true` 才启动 FaultManager |
| `enable_scale_p2d` | bool | 是否启用 ScaleP2D（用户侧默认 `false`） |
| `scale_p2d_d_instance_reinit_wait_timeout` | int | ScaleP2D 执行抢占前，等待 D 实例自恢复（重初始化）的最长时间（秒）。等待期间若 D 实例恢复为 `initial` / `active`，则不再执行 ScaleP2D；超时后若 D 实例仍处于 `inactive` 等可抢占状态，则继续后续 P 实例选择流程。默认：`60` |
| `strategy_center_check_interval` | int | 策略中心轮询间隔（秒） |

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

详见 [配置参考](../configuration/config_reference.md#motor_controller_config)。

### InferServiceSet YAML 配置（CRD 部署）

除上述 Controller 配置外，ScaleP2D 还依赖 InferServiceSet CRD 侧的**优先级调度**与**实例强制删除**能力：策略通过 NodeManager 停止 P 实例后，需由 CRD Controller 强制回收对应 Pod 并释放节点，供故障 D 实例恢复使用。

修改文件：`examples/deployer/yaml_template/infer_service_template.yaml`（CRD 模式下 deploy 脚本据此生成 `output_yamls/infer_service.yaml`）。

#### 1. 开启优先级调度

在 `InferServiceSet.spec.template` 下增加 `schedulingStrategy`，类型设为 `Priority`：

```yaml
spec:
  template:
    schedulingStrategy:
      type: Priority
    roles:
      # ...
```

#### 2. 为 prefill / decode 角色配置 priority

在 `prefill`、`decode` 两个 role 的 `spec` 同级增加 `priority` 字段（**仅开启优先级调度时生效**）：

| 字段 | 类型 | 取值范围 | 说明 |
|------|------|----------|------|
| `priority` | int | 1–32 | 数值越小，调度优先级越高 |

PD 分离场景下，建议 **prefill 的 `priority` 数值大于 decode**（即 prefill 优先级最低，更易被抢占），与 ScaleP2D「优先释放 P 算力」的策略一致。示例：

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

#### 3. 将 Pod 标签 fault-scheduling 改为 external-force

将 `prefill`、`decode` 角色 Pod 模板（`spec.template.metadata.labels`）中的 `fault-scheduling` 由默认的 `grace` 改为 `external-force`：

| 标签 | 修改前 | 修改后 | 说明 |
|------|--------|--------|------|
| `fault-scheduling` | `grace` | `external-force` | 开启实例级重调度；强制删除原实例并级联删除 Pod，供 ScaleP2D 实现 P 实例的强制释放 |

```yaml
        template:
          metadata:
            labels:
              fault-scheduling: external-force   # 原为 grace
              fault-retry-times: "10000"
              app: mindie-server
              # ...
```

## 日志与排查

日志前缀：`[motor/controller/fault_tolerance/scale_p2d]`

| 关键词 | 可能原因 | 建议 |
|--------|----------|------|
| `instance_not_in_instance_manager` | D 实例不存在 | 检查 ETCD / InstanceManager 同步 |
| `ScaleP2D not needed` + `initial/active` | D 未隔离 | 检查 `separate_instance` 流程 |
| `did not become INACTIVE` | 状态检查超时 | 检查隔离与状态上报延迟 |
| `Node metadata missing` | 节点未同步 | 检查 ResourceMonitor / pod_ip 映射 |
| `no_p_instances` | 无 P 实例 | 检查部署与注册 |
| `Insufficient Prefill nodes` | P 容量不足 | 扩容 P 或降低故障节点数 |
| `Failed to stop P instance node` | NodeManager 不可达 | 检查进程、网络、Pod 生命周期 |
| `algorithm_not_implemented` | 选择算法未实现 | 联系开发确认 P 实例选择策略 |

## 限制与约束

1. **策略范围**：仅 Decode 实例 + L4–L6 故障级别；L3 走其他策略或隔离逻辑。
2. **P 容量**：需保留至少 1 个 P 实例；可用节点不足时恢复失败。
3. **资源假设**：默认各 P 实例节点数相同。
4. **P 选择策略**：当前为占位实现（按实例 ID 排序），后续可能接入负载/优先级模型。
