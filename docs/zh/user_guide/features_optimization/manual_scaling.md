# 手动扩缩容

## 特性介绍

**依据原文上下文内容重组，请进行人工校验。**

手动扩缩容特性通过修改user_config.json中的实例数并执行相应命令，在已全量部署的集群基础上调整engine实例数量，实现扩容（增加实例）或缩容（减少实例）。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | **内容缺失，需要人工补齐**。 |
| 部署场景 | 支持PD分离（修改 `p_instances_num`、`d_instances_num`）和PD混部（修改 `hybrid_instances_num`）两种部署场景。 |
| 引擎 | **内容缺失，需要人工补齐**。 |
| 特性互斥 | **无显式说明，默认可与其他特性共存**。 |
| 软件依赖 | 需要kubectl权限。 |
| 其他限制 | <ul><li>扩缩容时仅允许修改 `motor_deploy_config.p_instances_num`、`motor_deploy_config.d_instances_num`、`motor_deploy_config.hybrid_instances_num` 字段；</li><li>实例数须大于0且不超过16，否则部署或扩缩容时会报错；</li><li>扩缩容只影响engine实例，controller/coordinator不会在扩缩容路径中更新；</li><li>如需修改镜像、挂载路径等非实例数配置，请进行重新部署；</li><li>缩容会从高index开始删除实例，并同步删除output/deployment/下对应的engine YAML文件；</li><li>已部署配置的基线为集群内ConfigMap（motor-config）中的user_config；</li><li>Prefix Cache特性默认开启，该特性会复用已计算好的KV Cache，用于提高推理性能，新扩容的实例没有KV Cache缓存，因此该实例的推理性能可能出现小幅度劣化并在一段时间后恢复。</li></ul> |

## 特性使用

### 环境准备

- 已参见[PD混部服务部署](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/user_guide/deployment/k8s/pd_aggregation_deployment.md)或[PD分离服务部署](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/user_guide/deployment/k8s/pd_disaggregation_deployment.md)完成环境部署。
- 已成功完成至少一次全量部署（集群内会存在ConfigMap motor-config，其中含当前已部署的user_config，作为基线）。
- 具备kubectl权限。

### 使用场景

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

#### 场景一：PD分离

适用于Prefill和Decode分离部署的场景，通过修改 `p_instances_num` 和 `d_instances_num` 调整各自实例数。

#### 场景二：PD混部

适用于Prefill和Decode混合部署的场景，通过修改 `hybrid_instances_num` 调整实例数。

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

#### 场景一：PD分离

1. 首次部署时，在examples/deployer目录下执行以下命令全量部署。

   ```bash
   cd examples/deployer
   # 方式一，指定配置目录（推荐）
   python3 deploy.py --config_dir ../infer_engines/vllm

   # 方式二，单独指定配置文件
   python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

   参数说明：
   - --config_dir：指定配置目录路径。
   - --user_config_path：单独指定user_config.json路径。
   - --env_config_path：单独指定env.json路径。

   执行完成后：
   - 集群中会创建/更新ConfigMap motor-config（内容来自当前输入的user_config.json），作为后续扩缩容与刷新的基线；
   - output/deployment/下会生成各服务YAML。

2. 根据实际情况修改user_config.json中的实例数，需要修改的参数为`p_instances_num`和`d_instances_num`。

   **依据原文上下文内容重组，请进行人工校验。**

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | motor_deploy_config.p_instances_num | 内容缺失，需要人工补齐。 | (0，,16] | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 |
   | motor_deploy_config.d_instances_num | 内容缺失，需要人工补齐。 | (0，,16] | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 |

   配置示例：**内容缺失，需要人工补齐。**

3. 在examples/deployer目录下执行扩缩容命令。

   ```bash
   python3 deploy.py --config_dir ../infer_engines/vllm --update_instance_num
   ```

   >[!NOTE] 说明
   >若使用单独指定配置文件方式部署，扩缩容时需同样指定 `--user_config_path` 和 `--env_config_path`。

   参数说明：
   - --update_instance_num：指定扩缩容模式，基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   - --config_dir：指定配置目录路径。
   - --user_config_path：单独指定user_config.json路径。
   - --env_config_path：单独指定env.json路径。

   >[!NOTE] 说明
   >- 基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   >- 扩容时仅对新增实例index执行kubectl apply，已运行实例不会被重新拉起。
   >- 缩容时从高index开始依次删除实例，并同步删除output/deployment/下对应的engine YAML文件。
   >- 执行扩缩容成功后ConfigMap会更新为当前输入的user_config.json。

#### 场景二：PD混部

1. 首次部署时，在examples/deployer目录下执行以下命令全量部署。

   ```bash
   cd examples/deployer

   # 方式一，指定配置目录（推荐）
   python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid

   # 方式二，单独指定配置文件
   python3 deploy.py --user_config_path ../infer_engines/vllm/pd_hybrid/user_config.json --env_config_path ../infer_engines/vllm/pd_hybrid/env.json
   ```

   参数说明：
   - --config_dir：指定配置目录路径。
   - --user_config_path：单独指定user_config.json路径。
   - --env_config_path：单独指定env.json路径。

   执行完成后：
   集群中会创建/更新ConfigMap motor-config（内容来自当前输入的user_config.json），作为后续扩缩容与刷新的基线；output/deployment/下会生成各服务YAML。

2. 根据实际情况修改user_config.json中的实例数，需要修改的参数为`hybrid_instances_num`。

   **依据原文上下文内容重组，请进行人工校验。**

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | motor_deploy_config.hybrid_instances_num | 内容缺失，需要人工补齐。 | (0，,16] | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 |

   配置示例：**内容缺失，需要人工补齐。**

3. 在examples/deployer目录下执行扩缩容命令。

   ```bash
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --update_instance_num
   ```

   >[!NOTE] 说明
   >若使用单独指定配置文件方式部署，扩缩容时需同样指定 `--user_config_path` 和 `--env_config_path`。

   参数说明：
   - --update_instance_num：指定扩缩容模式，基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   - --config_dir：指定配置目录路径。
   - --user_config_path：单独指定user_config.json路径。
   - --env_config_path：单独指定env.json路径。

   >[!NOTE] 说明
   >- 基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   >- 扩容时仅对新增实例index执行kubectl apply，已运行实例不会被重新拉起。
   >- 缩容时从高index开始依次删除实例，并同步删除output/deployment/下对应的engine YAML文件。
   >- 执行扩缩容成功后 ConfigMap 会更新为当前输入的 user_config.json。

### 验证特性

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

1. 检查ConfigMap是否已更新：

   ```bash
   kubectl get configmap motor-config -o yaml
   ```

   若ConfigMap motor-config存在，且其中的user_config已更新为当前输入的实例数。

2. 检查engine实例状态：

   ```bash
   kubectl get pod -A
   ```

   扩容后新增的engine实例处于Running状态；缩容后高index的实例已被删除。

## 常见问题

**依据原文上下文内容重组，请进行人工校验。**

### 问题一：ConfigMap motor-config not found or has no user_config in cluster

**问题描述**

执行扩缩容时报错"ConfigMap motor-config not found or has no user_config in cluster"。

**原因分析**

尚未进行过全量部署，或对应namespace下没有motor-config。

**解决步骤**

1. 在examples/deployer目录下执行全量部署：

   ```bash
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

### 问题二：user_config changes detected beyond instance numbers

**问题描述**

执行扩缩容时报错"user_config changes detected beyond instance numbers"。

**原因分析**

除实例数外还修改了其他配置。

**解决步骤**

1. 检查user_config.json，确保仅修改了 `p_instances_num`、`d_instances_num` 或 `hybrid_instances_num`，未修改其他配置项。
2. 如需对比基线配置，可查看集群内ConfigMap（motor-config）中的user_config。具体命令：**内容缺失，需要人工补齐。**
