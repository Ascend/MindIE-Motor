# 手动扩缩容

## 特性介绍

手动扩缩容特性通过修改`user_config.json`中的实例数并执行`--update_instance_num`，在已全量部署的集群上调整engine实例数量，实现扩容（增加实例）或缩容（减少实例）。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | <ul><li>Atlas 800I A2推理服务器</li><li>Atlas 800I A3超节点服务器</li></ul>手动扩缩容无额外硬件限制，与已部署服务使用的机型一致。 |
| 部署场景 | 支持PD分离（修改`p_instances_num`、`d_instances_num`）和PD混部（修改`hybrid_instances_num`）。 |
| 引擎 | 支持vLLM和SGLang；扩缩容不切换引擎，须与已部署服务的`engine_type`保持一致。 |
| 软件依赖 | 需要K8s集群管理员的kubectl权限，且集群内已存在全量部署生成的ConfigMap `motor-config`。 |
| 其他限制 | <ul><li>扩缩容时仅允许修改`motor_deploy_config.p_instances_num`、`motor_deploy_config.d_instances_num`、`motor_deploy_config.hybrid_instances_num`字段；</li><li>实例数须大于0且不超过16，否则部署或扩缩容时会报错；</li><li>扩缩容只影响engine实例，controller / coordinator不会在扩缩容路径中更新；</li><li>如需修改镜像、挂载路径等非实例数配置，请进行重新部署；</li><li>缩容会从高index开始删除实例，并同步删除`output/deployment/`下对应的engine YAML文件；</li><li>已部署配置的基线为集群内ConfigMap（motor-config）中的user_config；</li><li>Prefix Cache特性默认开启，该特性会复用已计算好的KV Cache。新扩容的实例没有KV Cache缓存，该实例的推理性能可能出现小幅度劣化并在一段时间后恢复。</li></ul> |

## 特性使用

### 环境准备

- 已参见[PD混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)、[PD分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md)或[SGLang PD分离服务部署](../deployment/k8s/pd_disaggregation_sglang.md)完成环境部署。
- 已成功完成至少一次全量部署（集群内会存在ConfigMap `motor-config`，其中含当前已部署的user_config，作为基线）。
- 具备K8s集群管理员的kubectl权限。

### 使用场景

**场景一：PD分离**

适用于Prefill和Decode分离部署的场景，通过修改`p_instances_num`和`d_instances_num`分别调整P、D实例数。

**场景二：PD混部**

适用于Prefill和Decode混合部署的场景，通过修改`hybrid_instances_num`调整union实例数。

### 使用样例

#### 场景一：PD分离

1. 首次部署时，在`examples/deployer`目录下执行以下命令全量部署。`{namespace}`须与`user_config.json`中的`job_id`一致。

   ```bash
   kubectl create namespace {namespace}
   cd examples/deployer
   # 方式一，指定配置目录（推荐）
   python3 deploy.py --config_dir ../infer_engines/vllm --nostep

   # 方式二，单独指定配置文件
   python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json --nostep
   ```

   参数说明：
   - `--config_dir`：指定配置目录路径。
   - `--user_config_path`：单独指定user_config.json路径。
   - `--env_config_path`：单独指定env.json路径。

   执行完成后：
   - 集群中会创建/更新ConfigMap `motor-config`（内容来自当前输入的user_config.json），作为后续扩缩容与刷新的基线；
   - 部署成功时日志出现`all deploy end.`和`Deploy complete.`。

2. 根据实际情况修改`user_config.json`中的实例数，需要修改的参数为`p_instances_num`和`d_instances_num`。更多参数解释请参见[user_config.json配置文件全量参数说明](../configuration/config_reference.md#motor_deploy_config)。

   **表 1** PD分离实例数参数说明

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | motor_deploy_config.p_instances_num | int | [1, 16] | 是 | 1 | Prefill实例个数。 |
   | motor_deploy_config.d_instances_num | int | [1, 16] | 是 | 1 | Decode实例个数。 |

   配置示例（将P实例从1扩到2，D实例保持1）：

   ```json
   {
     "motor_deploy_config": {
       "p_instances_num": 2,
       "d_instances_num": 1
     }
   }
   ```

3. 在`examples/deployer`目录下执行扩缩容命令。

   ```bash
   python3 deploy.py --config_dir ../infer_engines/vllm --update_instance_num --nostep
   ```

   >[!NOTE] 说明
   >若使用单独指定配置文件方式部署，扩缩容时需同样指定`--user_config_path`和`--env_config_path`。

   参数说明：
   - `--update_instance_num`：指定扩缩容模式，基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   - `--config_dir`：指定配置目录路径。
   - `--user_config_path`：单独指定user_config.json路径。
   - `--env_config_path`：单独指定env.json路径。

   扩缩容成功时日志出现`instance num update end.`。

   >[!NOTE] 说明
   >- 基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   >- 扩容时仅对新增实例index执行kubectl apply，已运行实例不会被重新拉起。
   >- 缩容时从高index开始依次删除实例，并同步删除`output/deployment/`下对应的engine YAML文件。
   >- 执行扩缩容成功后ConfigMap会更新为当前输入的user_config.json。

#### 场景二：PD混部

1. 首次部署时，在`examples/deployer`目录下执行以下命令全量部署。

   ```bash
   kubectl create namespace {namespace}
   cd examples/deployer

   # 方式一，指定配置目录（推荐）
   python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --nostep

   # 方式二，单独指定配置文件
   python3 deploy.py --user_config_path ../infer_engines/vllm/pd_hybrid/user_config.json --env_config_path ../infer_engines/vllm/pd_hybrid/env.json --nostep
   ```

   参数说明：
   - `--config_dir`：指定配置目录路径。
   - `--user_config_path`：单独指定user_config.json路径。
   - `--env_config_path`：单独指定env.json路径。

   执行完成后，集群中会创建/更新ConfigMap `motor-config`，作为后续扩缩容与刷新的基线。部署成功时日志出现`all deploy end.`和`Deploy complete.`。

2. 根据实际情况修改`user_config.json`中的实例数，需要修改的参数为`hybrid_instances_num`。

   **表 2** PD混部实例数参数说明

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | motor_deploy_config.hybrid_instances_num | int | [1, 16] | 是（混部场景） | 无 | union实例个数。未配置该字段时不是PD混部。 |

   配置示例（将union实例从1扩到2）：

   ```json
   {
     "motor_deploy_config": {
       "hybrid_instances_num": 2
     }
   }
   ```

3. 在`examples/deployer`目录下执行扩缩容命令。

   ```bash
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --update_instance_num --nostep
   ```

   >[!NOTE] 说明
   >若使用单独指定配置文件方式部署，扩缩容时需同样指定`--user_config_path`和`--env_config_path`。

   参数说明：
   - `--update_instance_num`：指定扩缩容模式，基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   - `--config_dir`：指定配置目录路径。
   - `--user_config_path`：单独指定user_config.json路径。
   - `--env_config_path`：单独指定env.json路径。

   扩缩容成功时日志出现`instance num update end.`。

   >[!NOTE] 说明
   >- 基线来自集群ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
   >- 扩容时仅对新增实例index执行kubectl apply，已运行实例不会被重新拉起。
   >- 缩容时从高index开始依次删除实例，并同步删除`output/deployment/`下对应的engine YAML文件。
   >- 执行扩缩容成功后ConfigMap会更新为当前输入的user_config.json。

### 验证特性

以下以PD分离将`p_instances_num`从1扩到2、再缩回1为例。命令中的`{namespace}`须替换为实际命名空间，且须与`job_id`一致。

1. 检查扩缩容命令是否成功。

   成功时deploy日志出现：

   ```text
   instance num update end.
   ```

2. 检查ConfigMap中的实例数是否已更新。

   ```bash
   kubectl get configmap motor-config -n {namespace} -o jsonpath='{.data.user_config\.json}' | python3 -c "import sys,json; d=json.load(sys.stdin); c=d['motor_deploy_config']; print('p_instances_num=', c.get('p_instances_num'), 'd_instances_num=', c.get('d_instances_num'))"
   ```

   扩容到2个P实例后回显示例如下：

   ```text
   p_instances_num= 2 d_instances_num= 1
   ```

   缩容回1个P实例后回显示例如下：

   ```text
   p_instances_num= 1 d_instances_num= 1
   ```

3. 检查engine实例状态。

   ```bash
   kubectl get pod -n {namespace}
   ```

   扩容后新增高index的engine实例处于Running状态，回显示例如下：

   ```text
   NAME                         READY   STATUS    RESTARTS   AGE
   vllm-0-controller-0-xxx      1/1     Running   0          1m
   vllm-0-coordinator-0-xxx     1/1     Running   0          1m
   vllm-0-decode-0-0            1/1     Running   0          1m
   vllm-0-prefill-0-0           1/1     Running   0          1m
   vllm-0-prefill-1-0           1/1     Running   0          20s
   ```

   缩容后高index的实例进入Terminating并被删除，回显示例如下：

   ```text
   NAME                         READY   STATUS        RESTARTS   AGE
   vllm-0-controller-0-xxx      1/1     Running       0          2m
   vllm-0-coordinator-0-xxx     1/1     Running       0          2m
   vllm-0-decode-0-0            1/1     Running       0          2m
   vllm-0-prefill-0-0           1/1     Running       0          2m
   vllm-0-prefill-1-0           1/1     Terminating   0          1m
   ```

4. 检查InferServiceSet角色副本数是否与配置一致。

   ```bash
   kubectl get inferserviceset vllm -n {namespace} -o jsonpath='{range .spec.template.roles[*]}{.name} replicas={.replicas}{"\n"}{end}'
   ```

   扩容后回显示例如下：

   ```text
   prefill replicas=2
   decode replicas=1
   ```

   缩容后回显示例如下：

   ```text
   prefill replicas=1
   decode replicas=1
   ```

   PD混部场景将上述检查中的`p_instances_num` / `prefill`替换为`hybrid_instances_num` / `union`即可。

## 常见问题

### ConfigMap motor-config not found or has no user_config in cluster

**问题描述**

执行扩缩容时报错`ConfigMap motor-config not found`或`has no user_config in cluster`。

**原因分析**

尚未进行过全量部署，或对应namespace下没有motor-config。

**解决步骤**

1. 确认命名空间已创建，且与`job_id`一致。
2. 在`examples/deployer`目录下执行全量部署。

   ```bash
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm --nostep
   ```

### user_config changes detected beyond instance numbers

**问题描述**

执行扩缩容时出现`user_config changes detected beyond instance numbers`。

**原因分析**

除实例数外还修改了其他配置。扩缩容路径只允许改`p_instances_num`、`d_instances_num`或`hybrid_instances_num`。

**解决步骤**

1. 检查`user_config.json`，确保仅修改了`p_instances_num`、`d_instances_num`或`hybrid_instances_num`，未修改镜像、挂载路径、并行度等其他配置项。
2. 如需对比基线配置，可查看集群内ConfigMap（motor-config）中的user_config。

   ```bash
   kubectl get configmap motor-config -n {namespace} -o jsonpath='{.data.user_config\.json}' | python3 -m json.tool
   ```

3. 如需修改非实例数配置，请进行全量重新部署，不要使用`--update_instance_num`。
