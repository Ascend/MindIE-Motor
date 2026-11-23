# 部署失败定位指南

## 部署失败案例总览

请按部署失败现象快速定位问题根因，再按照对应解决方案修复此问题。

<table>
  <thead>
    <tr>
      <th>分类</th>
      <th>编号</th>
      <th>失败现象</th>
      <th>部署场景</th>
      <th>部署失败问题</th>
      <th>解决方案</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="4">Deployer 阶段</td>
      <td>1</td>
      <td>Deployer 在创建 ConfigMap 时中断，报 <code>namespaces "mindie-motor" not found</code></td>
      <td>Kubernetes 资源下发</td>
      <td><strong><a href="#deployer-namespace-not-created">namespace 未创建</a></strong></td>
      <td>创建 namespace，并确认 job_id 与其一致</td>
    </tr>
    <tr>
      <td>2</td>
      <td>Controller、Coordinator 或引擎 Pod 为 <code>0/1 ImagePullBackOff</code>，无法启动容器</td>
      <td>Kubernetes 镜像拉取</td>
      <td><strong><a href="#deployer-missing-mindie-image">缺少 mindie 镜像</a></strong></td>
      <td>在所有可能调度到的节点准备相同镜像</td>
    </tr>
    <tr>
      <td>3</td>
      <td>引擎 Pod 长期为 <code>0/1 Pending</code>，尚未分配节点和 IP，Motor 进程尚未运行；需进一步检查资源和事件</td>
      <td>Kubernetes 引擎 Pod 调度</td>
      <td><strong><a href="#deployer-insufficient-resources">资源不足</a></strong></td>
      <td>检查 NPU 占用、磁盘空间及网络、K8s、MindCluster 状态</td>
    </tr>
    <tr>
      <td>4</td>
      <td>引擎 Pod 为 <code>0/1 Pending</code>；<code>describe pod</code> 事件明确提示 <code>didn't match Pod's node affinity/selector</code></td>
      <td>Kubernetes 指定节点部署</td>
      <td><strong><a href="#deployer-node-selector-mismatch">nodeSelector 调度失败</a></strong></td>
      <td>对齐节点选择器与节点 label</td>
    </tr>
    <tr>
      <td rowspan="6">控制面启动与注册阶段</td>
      <td>1</td>
      <td>Controller Pod 为 <code>0/1 CrashLoopBackOff</code>，日志报 <code>OverflowError: bind(): port must be 0-65535</code>；其他 Pod 可正常运行</td>
      <td>Kubernetes 控制面启动</td>
      <td><strong><a href="#control-plane-controller-startup">Controller 启动失败</a></strong></td>
      <td>改为合法端口，默认 1026，再部署</td>
    </tr>
    <tr>
      <td>2</td>
      <td>Coordinator Pod 为 <code>0/1 CrashLoopBackOff</code>，日志报 <code>coordinator_api_infer_port must be in range 1-65535</code></td>
      <td>Kubernetes 控制面启动</td>
      <td><strong><a href="#control-plane-coordinator-startup">Coordinator 启动失败</a></strong></td>
      <td>改为合法端口，默认 1025，再部署</td>
    </tr>
    <tr>
      <td>3</td>
      <td>Pod 可显示 <code>1/1 Running</code>，但 NodeManager 日志中 <code>Device Count: 0</code>、<code>Endpoint Count: 0</code>，vLLM 未拉起，推理返回 <code>Service is not available</code></td>
      <td>大 EP / 多 Endpoint；文中以单卡 TP=2 的 1P1D 配置为例</td>
      <td><strong><a href="#control-plane-endpoint-readiness">大 EP / 多 Endpoint 场景 readiness 失败</a></strong></td>
      <td>对齐并行配置与可用卡数；示例中 TP 改为 1</td>
    </tr>
    <tr>
      <td>4</td>
      <td>Prefill（P）实例反复退出或重建，Decode（D）正常；P 日志报 log_level 非法，推理持续返回 HTTP 503 / <code>Service is not available</code></td>
      <td>Kubernetes PD 分离（仅 P 侧异常）</td>
      <td><strong><a href="#control-plane-prefill-readiness">Readiness / liveness 探针长期 false、503 连发</a></strong></td>
      <td>将 Prefill 的 log_level 改为 INFO，或删除该配置后再部署</td>
    </tr>
    <tr>
      <td>5</td>
      <td>引擎 Pod 反复退出或重启，Controller、Coordinator 可正常运行；NodeManager 日志报 <code>log_level must be one of: DEBUG, INFO, WARNING, ERROR</code></td>
      <td>Kubernetes 引擎 Pod 启动</td>
      <td><strong><a href="#control-plane-node-manager-startup">NodeManager 启动失败</a></strong></td>
      <td>改为合法日志级别，默认 INFO，或删除该配置后再部署</td>
    </tr>
    <tr>
      <td>6</td>
      <td>所有 Pod 可显示 <code>1/1 Running</code>，但部署一直等待引擎 ready（示例停在 10%），推理返回 HTTP 503 / <code>Service is not available</code></td>
      <td>Kubernetes PD 分离（向 Controller 注册）</td>
      <td><strong><a href="#control-plane-controller-registration">向 Controller 注册失败</a></strong></td>
      <td>删除错误配置，或改为实际 Controller Service 名后再部署</td>
    </tr>
    <tr>
      <td rowspan="1">推理引擎阶段</td>
      <td>1</td>
      <td>vLLM 日志报权重路径相关 <code>HFValidationError</code> / <code>OSError</code> 并启动失败；随后 NodeManager 连接引擎端口被拒绝并退出</td>
      <td>Kubernetes 推理引擎加载权重</td>
      <td><strong><a href="#inference-engine-invalid-model-path">user_config 类：错误权重路径</a></strong></td>
      <td>将 prefill / decode 的 model 改为实际权重目录</td>
    </tr>
  </tbody>
</table>

## 部署组件与链路

Motor 共 4 个组件：

- **Deployer**：基于 Kubernetes 的部署脚本工具，读 `user_config`、生成 YAML、`kubectl apply` 下发。namespace / 镜像 / Pending / 网络 / NPU / 磁盘 / MindCluster 等环境类问题都归本阶段。
- **Controller**：集群控制面。NodeManager 注册后，由它组装 P/D 实例并下发身份（StartCmd），再收心跳、做故障隔离。
- **Coordinator**：推理请求入口与调度。部署成功以推理请求 curl 通为准，不以 Pod 状态码为准。
- **NodeManager**：每个引擎 Pod 里的节点代理。向 Controller 注册、收身份、直接拉起本机 vLLM 进程并上报健康。

推理引擎为 **vLLM**，**vLLM-Ascend** 是使 vLLM 运行在昇腾 NPU 上的插件。Engine Server 已日落，现为原生拉起：NodeManager 直接执行 `vllm serve`，中间不再插 Engine Server 进程。

部署链路：Deployer → K8s Pod → Controller / Coordinator / NodeManager → vLLM（含权重加载到显存）。

按阶段对照现象：k8s pod、k8s node、拉起界面、showlog、显存。

## 部署成功示例

**部署成功（即服务已ready）以推理请求 curl 通为准。** Pod 状态码 `1/1` 没有代表性：引擎没有 readiness 探针，Coordinator `/readiness` 无实例时仍可能 HTTP 200。

下面为部署成功可以看到的信息。所有 pod 状态码为 1/1 只是常见伴随现象，不是判据。

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor              mindie-motor-controller-7f4665d8dd-xxxxx    1/1     Running            0              9m41s   192.168.1.10    node-1                  <none>           <none>
mindie-motor              mindie-motor-coordinator-5d49bd65fd-xxxxx   1/1     Running            0              9m40s   192.168.1.11    node-2                  <none>           <none>
mindie-motor              vllm-d0-7cddfd5f6d-xxxxx                    1/1     Running            0              9m40s   192.168.1.12    node-3                  <none>           <none>
mindie-motor              vllm-p0-8bb7795-xxxxx                       1/1     Running            0              9m40s   192.168.1.14    node-3                  <none>           <none>
```

部署界面打印信息

```text
2026-08-26 16:39:38,832 - INFO - all deploy end.
2026-08-26 16:39:38,832 - INFO - Deploy complete.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Waiting for pod running: [3/3]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[vllm-d0-7cddfd5f6d-xxxxx], line:[349], err:[0][_____]: 100%|███████████████████████████████████████████████████████| 100/100 [03:39<00:00]
[vllm-p0-8bb7795-xxxxx], line:[310], err:[0][_____]: 100%|██████████████████████████████████████████████████████████| 100/100 [03:39<00:00]
```

对应npu显存加载完成

```text
+===========================+===============+======================================================================+
+---------------------------+---------------+----------------------------------------------------------------------+
| NPU     Chip              | Process id    | Process name       | Process memory(MB)    | Process id in container |
+===========================+===============+======================================================================+
| 0       0                 | 10001         | VLLMWorker_TP      | 56574                 | NA                      |
+===========================+===============+======================================================================+
| 1       0                 | 10002         | VLLMWorker_TP      | 56574                 | NA                      |
+===========================+===============+======================================================================+
```

show_log中coordinator日志打印

```text
(SchedulerServer pid=142) INFO 08-26 16:43:46 [domain][instance_manager.py:357] Refresh instances done: E=0, P=1, D=2, U=0
```

推理请求 curl 通（成功判据）

```bash
[root@node-1]# curl http://192.168.1.1:31015/v1/completions -H "Content-Type: application/json" -d '{"model":"qwen","prompt":"who are you?","max_tokens":32}'

{"id":"cmpl-xxxxxxxxxxxxxxxx","object":"text_completion","created":1787744836,"model":"qwen","choices":[{"index":0,"text":" I am a large language model created by Alibaba Cloud. I am called Qwen. How can I assist you today?","logprobs":null,"finish_reason":"stop","stop_reason":151643,"prompt_logprobs":null,"routed_experts":null}],"service_tier":null,"system_fingerprint":"vllm-0.23.0-xxxxxxxx","usage":{"prompt_tokens":4,"total_tokens":29,"completion_tokens":25,"prompt_tokens_details":{"cached_tokens":0}},"kv_transfer_params":null}
```

## Deployer 阶段部署失败

<a id="deployer-namespace-not-created"></a>

### **1、namespace 未创建**

**问题现象**

部署界面打印：

```text
2026-08-26 19:52:45,447 - INFO - [NodePort] summary: infer=30105 coordinator_obs=30106 controller_obs=30104
Traceback (most recent call last):
  File "/mnt/user/motor_deploy/deployer/deploy.py", line 489, in <module>
    main()
  File "/mnt/user/motor_deploy/deployer/deploy.py", line 479, in main
    deploy_services(user_config, env_config_path, dry_run=args.dry_run, auto_log_collect=args.auto_log_collect)
  File "/mnt/user/motor_deploy/deployer/deploy.py", line 251, in deploy_services
    deploy_services_multi_yaml(paths, user_config, dry_run=dry_run)
  File "/mnt/user/motor_deploy/deployer/deploy.py", line 183, in deploy_services_multi_yaml
    exec_all_kubectl_multi(deploy_config, None, C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML, user_config=user_config)
  File "/mnt/user/motor_deploy/deployer/lib/generator/k8s_utils.py", line 582, in exec_all_kubectl_multi
    create_motor_config_configmap(job_id, user_config=user_config, effective_deploy_mode=deploy_mode_arg)
  File "/mnt/user/motor_deploy/deployer/lib/generator/k8s_utils.py", line 533, in create_motor_config_configmap
    apply_configmap(
  File "/mnt/user/motor_deploy/deployer/lib/generator/k8s_utils.py", line 404, in apply_configmap
    pipe_kubectl(create_cmd)
  File "/mnt/user/motor_deploy/deployer/lib/utils.py", line 98, in pipe_kubectl
    raise RuntimeError(
RuntimeError: kubectl apply configmap failed (rc=1): Error from server (NotFound): error when creating "STDIN": namespaces "mindie-motor" not found
```

**定位思路/可能原因**

`kubectl apply` 创建 ConfigMap 时，目标 namespace 不存在。

**解决方案**

创建 namespace（在容器外执行）：

```bash
kubectl create ns mindie-motor
```

user_config 中修改 job_id：

```text
"job_id": "mindie-motor",
```

<a id="deployer-missing-mindie-image"></a>

### **2、缺少 mindie 镜像**

**问题现象**

k8s命令查询pods显示

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor                mindie-motor-controller-76c646cb96-xxxxx    0/1     ImagePullBackOff   0              13s     192.168.1.20    node-2                  <none>           <none>
mindie-motor                mindie-motor-coordinator-7868754758-xxxxx   0/1     ImagePullBackOff   0              12s     192.168.1.21    node-2                  <none>           <none>
mindie-motor                vllm-d0-674cb9d6f6-xxxxx                    0/1     ImagePullBackOff   0              12s     192.168.1.22    node-3                  <none>           <none>
mindie-motor                vllm-d1-bf9788ccc-xxxxx                     0/1     ImagePullBackOff   0              12s     192.168.1.23    node-3                  <none>           <none>
mindie-motor                vllm-p0-6df788bd76-xxxxx                    0/1     ImagePullBackOff   0              12s     192.168.1.24    node-3                  <none>           <none>
```

**定位思路/可能原因**

Pod 所在节点拉不到 `user_config` 里配置的镜像。

**解决方案**

需要给集群所有跑到的机器全部都装上对应的同一镜像。

<a id="deployer-insufficient-resources"></a>

### **3、资源不足**

**问题现象**

拉起服务后，server pod 出现 Pending：

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor            mindie-motor-controller-74b4654779-xxxxx    0/1     Running            0              10s     192.168.1.30    node-2                  <none>           <none>
mindie-motor            mindie-motor-coordinator-6db9958557-xxxxx   0/1     Running            0              9s      192.168.1.31    node-2                  <none>           <none>
mindie-motor            vllm-d0-7dfdd9dc74-xxxxx                    0/1     Pending            0              9s      <none>           <none>                  <none>           <none>
mindie-motor            vllm-d1-7594754959-xxxxx                    0/1     Pending            0              9s      <none>           <none>                  <none>           <none>
mindie-motor            vllm-p0-69b8846dd4-xxxxx                    0/1     Pending            0              9s      <none>           <none>                  <none>           <none>
```

**定位思路/可能原因**

此时 MindIE-Motor 未被实际执行。可能的原因是：

1. 对应 npu 被其他任务占用
2. 机器空间不足
3. 环境问题

以上均属 Deployer 阶段，不单列环境阶段。

**解决方案**

到对应机器上执行：

```bash
npu-smi info
```

确认 NPU 是否被占用。

在对应机器查询存储空间，/home、/root、公共盘/mnt：

```bash
df -h /home
df -h /root
df -h /mnt
```

空间满了或者接近满则会造成拉起时 pod 为 Pending 状态。

环境问题则有可能是机器网络问题、npu 与 cpu 状态、k8s 组件问题、mindcluster 问题等。

<a id="deployer-node-selector-mismatch"></a>

### **4、nodeSelector 调度失败**

**问题现象**

拉起服务后，server pod 出现 Pending，k8s 查询现象与资源不足相同：

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor            mindie-motor-controller-74b4654779-xxxxx    0/1     Running            0              10s     192.168.1.30    node-2                  <none>           <none>
mindie-motor            mindie-motor-coordinator-6db9958557-xxxxx   0/1     Running            0              9s      192.168.1.31    node-2                  <none>           <none>
mindie-motor            vllm-d0-7dfdd9dc74-xxxxx                    0/1     Pending            0              9s      <none>           <none>                  <none>           <none>
mindie-motor            vllm-d1-7594754959-xxxxx                    0/1     Pending            0              9s      <none>           <none>                  <none>           <none>
mindie-motor            vllm-p0-69b8846dd4-xxxxx                    0/1     Pending            0              9s      <none>           <none>                  <none>           <none>
```

**定位思路/可能原因**

此时 MindIE-Motor 未被实际执行。先按资源不足排查 npu、磁盘后，到对应 pending 的 pod 上 describe，确认是不是选错机器：

```bash
kubectl describe pod -n mindie-motor vllm-p0-xxxxx
```

如果事件里出现类似下面内容，则是 nodeSelector 调度失败，不是 NPU 被占或磁盘满：

```text
0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector.
```

**解决方案**

检查 user_config 中的 `prefill_node_selector` / `decode_node_selector`（以及 controller、coordinator 的 node_selector），与集群节点 label 是否一致。

```bash
kubectl get nodes --show-labels
```

## Controller/Coordinator/NodeManager 控制面启动与注册失败

<a id="control-plane-controller-startup"></a>

### **1、Controller 启动失败**

**问题现象**

k8s 查询 pods 显示，仅 controller 异常，coordinator 与引擎仍为 1/1：

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor                mindie-motor-controller-5b565dd99c-xxxxx   0/1     CrashLoopBackOff   3 (25s ago)     2m11s   192.168.1.20   node-2             <none>           <none>
mindie-motor                mindie-motor-coordinator-d8567c954-xxxxx   1/1     Running            0               2m11s   192.168.1.21   node-2             <none>           <none>
mindie-motor                vllm-d0-6bd7477df4-xxxxx                   1/1     Running            0               2m10s   192.168.1.22   node-3             <none>           <none>
mindie-motor                vllm-p0-65cc85fcc5-xxxxx                   1/1     Running            0               2m10s   192.168.1.23   node-3             <none>           <none>
```

showlog 中 controller 打印：

```text
Current node role: ROLE=controller
(MainProcess pid=30) INFO 08-28 17:58:05 [config][config_utils.py:414] Loading configuration file: /usr/local/Ascend/pyMotor/conf/user_config.json
(MainProcess pid=30) INFO 08-28 17:58:05 [config][config_utils.py:416] Successfully loaded configuration file: /usr/local/Ascend/pyMotor/conf/user_config.json
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/controller/main.py", line 47, in <module>
    exit_code = main()
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/controller/main.py", line 40, in main
    run_port_setup_or_exit(apply_controller_ports, config)
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/common/utils/port_allocator.py", line 324, in run_port_setup_or_exit
    apply_fn(config)
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/common/utils/port_allocator.py", line 254, in apply_controller_ports
    api.controller_api_port = PortAllocator.allocate_strict(
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/common/utils/port_allocator.py", line 90, in allocate_strict
    if PortAllocator.probe_tcp(host, port, timeout=timeout):
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/common/utils/port_allocator.py", line 80, in probe_tcp
    sock.bind((bind_host, port))
OverflowError: bind(): port must be 0-65535.
```

**定位思路/可能原因**

`controller_api_port` 是 Controller 管理面监听端口，默认 `1026`，一般无需修改。写成超出 TCP 端口范围的值（例如 `99999`）时，Controller 进程在绑口阶段退出。

```json
"motor_controller_config": {
  "api_config": {
    "controller_api_port": 99999
  }
}
```

**解决方案**

将 `controller_api_port` 改回合法端口（默认 `1026`），重新部署。

<a id="control-plane-coordinator-startup"></a>

### **2、Coordinator 启动失败**

**问题现象**

k8s 查询 pods 显示，仅 coordinator 异常，controller 与引擎仍为 1/1：

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor                mindie-motor-controller-86bbf8f8-xxxxx      1/1     Running            0               5m15s   192.168.1.20   node-2             <none>           <none>
mindie-motor                mindie-motor-coordinator-847764c579-xxxxx   0/1     CrashLoopBackOff   5 (35s ago)     5m14s   192.168.1.21   node-2             <none>           <none>
mindie-motor                vllm-d0-67b88d9d74-xxxxx                    1/1     Running            0               5m14s   192.168.1.22   node-3             <none>           <none>
mindie-motor                vllm-p0-bb66b56f-xxxxx                      1/1     Running            0               5m14s   192.168.1.23   node-3             <none>           <none>
```

showlog 中 coordinator 打印：

```text
Current node role: ROLE=coordinator
(MainProcess pid=128) INFO 08-28 19:56:06 [config][coordinator.py:480] Using encryption algorithm: PBKDF2_SHA256
(MainProcess pid=128) ERROR 08-28 19:56:06 [config][coordinator.py:1026] Configuration validation failed:
(MainProcess pid=128) ERROR 08-28 19:56:06 [config][coordinator.py:1026]   - coordinator_api_infer_port must be in range 1-65535
(MainProcess pid=128) ERROR 08-28 19:56:06 [config][coordinator.py:811] Failed to create configuration instance: Configuration validation failed:
(MainProcess pid=128) ERROR 08-28 19:56:06 [config][coordinator.py:811]   - coordinator_api_infer_port must be in range 1-65535
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/coordinator/main.py", line 16, in <module>
    from motor.coordinator.daemon.coordinator_daemon import CoordinatorDaemon
  ...
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/coordinator/api_client/conductor_api_client.py", line 62, in ConductorApiClient
    coordinator_config = CoordinatorConfig.from_json()
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/config/coordinator.py", line 801, in from_json
    config.validate_config()
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/config/coordinator.py", line 1027, in validate_config
    raise ValueError(error_msg)
ValueError: Configuration validation failed:
  - coordinator_api_infer_port must be in range 1-65535
```

**定位思路/可能原因**

`coordinator_api_infer_port` 是 Coordinator 推理面监听端口，默认 `1025`，一般无需修改。写成超出 TCP 端口范围的值（例如 `99999`）时，Coordinator 在配置校验阶段退出（与 Controller 在绑口阶段退出不同）。

```json
"motor_coordinator_config": {
  "api_config": {
    "coordinator_api_infer_port": 99999
  }
}
```

**解决方案**

将 `coordinator_api_infer_port` 改回合法端口（默认 `1025`），重新部署。

<a id="control-plane-endpoint-readiness"></a>

### **3、大 EP / 多 Endpoint 场景 readiness 失败**

**问题现象**

k8s 查询 pods 显示全部 1/1，状态码没有代表性：

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor             mindie-motor-controller-85757d5469-xxxxx    1/1     Running            0              4m44s   192.168.1.20   node-2             <none>           <none>
mindie-motor             mindie-motor-coordinator-7b459ddb89-xxxxx   1/1     Running            0              4m43s   192.168.1.21   node-2             <none>           <none>
mindie-motor             vllm-d0-7466b6999f-xxxxx                    1/1     Running            0              4m43s   192.168.1.22   node-3             <none>           <none>
mindie-motor             vllm-p0-7b856f59f8-xxxxx                    1/1     Running            0              4m43s   192.168.1.23   node-3             <none>           <none>
```

showlog 中引擎节点打印 ERROR 后仍继续启动，`Device Count` / `Endpoint Count` 为 0，Port Matrix 没有 NativeEngine 业务口，随后仍可能 `Register success`：

```text
Current node role: ROLE=decode
(MainProcess pid=237) INFO 08-28 21:59:56 [config][node_manager.py:788] Using 1 devices from config for role decode
(MainProcess pid=237) ERROR 08-28 21:59:56 [config][node_manager.py:692] Failed to get device count from config: Device count (1) must bigger than or equal to devices per dp (2) and dp must be bigger than 0
(MainProcess pid=237) INFO 08-28 21:59:56 [config][node_manager.py:435] Configuration loading completed
...
(NodeManager pid=237) INFO 08-28 21:59:56 [utils][port_allocator.py:48] [Port Matrix] NodeManager   0.0.0.0         1026    TCP     auto        NM API
...
(NodeManager pid=237) INFO 08-28 21:59:56 [utils][config_runtime.py:28]     ├─ Device Count:        0
(NodeManager pid=237) INFO 08-28 21:59:56 [utils][config_runtime.py:28]     ├─ Multi Endpoints:     True
(NodeManager pid=237) INFO 08-28 21:59:56 [utils][config_runtime.py:28]     ├─ Endpoint Count:      0
...
(NodeManager pid=237) INFO 08-28 21:59:56 [utils][config_runtime.py:28]     ├─ TP Size:          TP=2
(NodeManager pid=237) INFO 08-28 21:59:56 [utils][config_runtime.py:28]     └─ World Size:       World Size=2
...
(NodeManager pid=237) INFO 08-28 21:59:56 [node_manager][controller_api_client.py:97] Register success!
```

没有拉起 vLLM。推理请求 curl 不通：

```bash
[root@node-1]# curl http://192.168.1.1:30101/v1/completions -H "Content-Type: application/json" -d '{"model":"qwen","prompt":"who are you?","max_tokens":32}'

{"detail":"Service is not available"}
```

**定位思路/可能原因**

多 Endpoint 要求卡数不少于每个 DP 的设备数（`device_num >= devices_per_dp`，其中 `devices_per_dp = TP × PP × PCP`）。1P1D 上把 `tensor_parallel_size` 写成大于 `p_pod_npu_num` / `d_pod_npu_num`（例如卡数 1、TP=2）时，NodeManager 不会退出，只把 device / endpoint 置 0，vLLM 拉不起来。完整多卡大 EP 另需对应规模，本条是同一校验在小规模上的现象。

```json
"motor_deploy_config": {
  "p_pod_npu_num": 1,
  "d_pod_npu_num": 1
},
"motor_engine_prefill_config": {
  "engine_config": {
    "tensor_parallel_size": 2
  }
},
"motor_engine_decode_config": {
  "engine_config": {
    "tensor_parallel_size": 2
  }
}
```

**解决方案**

将 `tensor_parallel_size` 改回与卡数一致（本环境为 `1`），重新部署。

<a id="control-plane-prefill-readiness"></a>

### **4、Readiness / liveness 探针长期 false、503 连发**

**问题现象**

PD 分离下 Coordinator 需要同时有可用的 Prefill 与 Decode。只把 Prefill 配挂（Decode 保持正常）时，引擎 Pod 不会全部 CrashLoop：D 仍为 1/1，P 反复退出。Coordinator 进程也仍为 1/1（`/readiness` 无实例时仍可能 HTTP 200），但推理返回 503。

k8s 查询 pods 显示仅 P 异常，D 与控制面仍为 1/1。重启瞬间可能是 Terminating 叠加 Pending：

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor             mindie-motor-controller-66666898bb-xxxxx    1/1     Running            0              86s     192.168.1.20   node-2             <none>           <none>
mindie-motor             mindie-motor-coordinator-7754c9c4f6-xxxxx   1/1     Running            0              86s     192.168.1.21   node-2             <none>           <none>
mindie-motor             vllm-d0-65bf54dcd7-xxxxx                    1/1     Running            0              85s     192.168.1.22   node-3             <none>           <none>
mindie-motor             vllm-p0-7cd465bd7d-xxxxx                    0/1     Pending            0              5s      <none>           <none>                  <none>           <none>
mindie-motor             vllm-p0-7cd465bd7d-xxxxx                    1/1     Terminating        1 (76s ago)    57s     192.168.1.23   node-3             <none>           <none>
```

引擎 Pod 默认没有 readiness 探针，P 被拉起来后状态码会再次变成 1/1，不要只看 Running。对比 AGE：P 明显短于 D 和控制面，说明刚重启过。

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor             mindie-motor-controller-66666898bb-xxxxx    1/1     Running            0              2m56s   192.168.1.20   node-2             <none>           <none>
mindie-motor             mindie-motor-coordinator-7754c9c4f6-xxxxx   1/1     Running            0              2m56s   192.168.1.21   node-2             <none>           <none>
mindie-motor             vllm-d0-65bf54dcd7-xxxxx                    1/1     Running            0              2m55s   192.168.1.22   node-3             <none>           <none>
mindie-motor             vllm-p0-7cd465bd7d-xxxxx                    1/1     Running            0              41s     192.168.1.23   node-3             <none>           <none>
```

P 节点 showlog 与「NodeManager 启动失败」相同，角色为 prefill：

```text
Current node role: ROLE=prefill
(MainProcess pid=250) ERROR 08-28 21:42:40 [config][config_utils.py:434] Configuration validation failed:
(MainProcess pid=250) ERROR 08-28 21:42:40 [config][config_utils.py:434]   - log_level must be one of: DEBUG, INFO, WARNING, ERROR
ValueError: Configuration validation failed:
  - log_level must be one of: DEBUG, INFO, WARNING, ERROR
```

coordinator showlog 可以没有任何 ERROR。推理请求返回 503：

```bash
[root@node-1]# curl http://192.168.1.1:30101/v1/completions -H "Content-Type: application/json" -d '{"model":"qwen","prompt":"who are you?","max_tokens":32}'

{"detail":"Service is not available"}
```

**定位思路/可能原因**

这与「向 Controller 注册失败」的 503 相同，都是 Coordinator 没有凑齐可服务实例；区别是这里 D 已起来、只有 P 挂。只把 Prefill 的 `log_level` 配成非法值即可复现：

```json
"motor_engine_prefill_config": {
  "motor_nodemanger_config": {
    "logging_config": {
      "log_level": "FOO"
    }
  }
}
```

**解决方案**

将 Prefill 的 `log_level` 改回 `INFO`（或删掉该 `motor_nodemanger_config`）后重新部署。

<a id="control-plane-node-manager-startup"></a>

### **5、NodeManager 启动失败**

**问题现象**

k8s 查询 pods 显示 controller、coordinator 仍为 1/1，引擎 Pod 反复退出。抓到重启瞬间可能是 Terminating 叠加 Pending；引擎没有 readiness 探针，过一会儿也可能再次显示 1/1，此时看 AGE / RESTARTS，不要只看状态码。

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor             mindie-motor-controller-79f9fb77d6-xxxxx   1/1     Running            0              2m20s   192.168.1.20   node-2             <none>           <none>
mindie-motor             mindie-motor-coordinator-ccb94b47c-xxxxx   1/1     Running            0              2m19s   192.168.1.21   node-2             <none>           <none>
mindie-motor             vllm-d0-7947df56cf-xxxxx                   1/1     Terminating        1 (77s ago)    57s     192.168.1.22   node-3             <none>           <none>
mindie-motor             vllm-d0-7947df56cf-xxxxx                   0/1     Pending            0              5s      <none>           <none>                  <none>           <none>
mindie-motor             vllm-p0-74b8bddd94-xxxxx                   1/1     Terminating        1 (77s ago)    58s     192.168.1.23   node-3             <none>           <none>
mindie-motor             vllm-p0-74b8bddd94-xxxxx                   0/1     Pending            0              5s      <none>           <none>                  <none>           <none>
```

showlog 中引擎节点（NodeManager）打印：

```text
Current node role: ROLE=decode
(MainProcess pid=250) INFO 08-28 21:26:44 [config][node_manager.py:663] Role from environment: decode
(MainProcess pid=250) INFO 08-28 21:26:44 [config][node_manager.py:664] Role from config: PDRole.ROLE_D
(MainProcess pid=250) ERROR 08-28 21:26:44 [config][config_utils.py:434] Configuration validation failed:
(MainProcess pid=250) ERROR 08-28 21:26:44 [config][config_utils.py:434]   - log_level must be one of: DEBUG, INFO, WARNING, ERROR
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/node_manager/main.py", line 22, in <module>
    from motor.node_manager.node_manager import NodeManager
  ...
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/node_manager/api_client/controller_api_client.py", line 75, in ControllerApiClient
    nodemanager_config = NodeManagerConfig.from_json()
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/config/node_manager.py", line 430, in from_json
    config.validate_config()
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/config/node_manager.py", line 863, in validate_config
    raise_if_config_errors(errors)
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/motor/config/config_utils.py", line 435, in raise_if_config_errors
    raise ValueError(error_msg)
ValueError: Configuration validation failed:
  - log_level must be one of: DEBUG, INFO, WARNING, ERROR
```

**定位思路/可能原因**

`log_level` 合法值为 `DEBUG`、`INFO`、`WARNING`、`ERROR`，默认 `INFO`，一般无需修改。写成非法值（例如 `FOO`）时，NodeManager 在配置校验阶段退出。该字段写在 P/D 引擎配置下的 `motor_nodemanger_config` 中（注意键名拼写）。

```json
"motor_engine_prefill_config": {
  "motor_nodemanger_config": {
    "logging_config": {
      "log_level": "FOO"
    }
  }
},
"motor_engine_decode_config": {
  "motor_nodemanger_config": {
    "logging_config": {
      "log_level": "FOO"
    }
  }
}
```

**解决方案**

将 `log_level` 改回合法值（默认 `INFO`），或删除 `motor_nodemanger_config` 中的该配置后重新部署。

<a id="control-plane-controller-registration"></a>

### **6、向 Controller 注册失败**

**问题现象**

k8s 查询 pods 显示全部 1/1。引擎 Pod 默认没有 readiness 探针，`1/1` 只说明容器在跑，不等于实例已注册、不等于可以推理。

```bash
[root@node-1 user]# kubectl get pods -A -owide | grep mindie-motor
mindie-motor             mindie-motor-controller-7787454458-xxxxx    1/1     Running            0              6m51s   192.168.1.20   node-2             <none>           <none>
mindie-motor             mindie-motor-coordinator-7f9b687f98-xxxxx   1/1     Running            0              6m51s   192.168.1.21   node-2             <none>           <none>
mindie-motor             vllm-d0-f5bc4694-xxxxx                      1/1     Running            0              6m50s   192.168.1.22   node-3             <none>           <none>
mindie-motor             vllm-p0-777dd6c7d-xxxxx                     1/1     Running            0              6m50s   192.168.1.23   node-3             <none>           <none>
```

部署界面会一直停在等待引擎 ready，进度不再往上走：

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Waiting for pod running: [2/2]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[vllm-d0-f5bc4694-xxxxx], line:[409], err:[94][_____]:  10%|█████▊                                                    | 10/100
[vllm-p0-777dd6c7d-xxxxx], line:[409], err:[94][_____]:  10%|█████▋                                                   | 10/100
```

coordinator showlog 可以没有任何 ERROR。推理请求返回 503：

```bash
[root@node-1]# curl http://192.168.1.1:30101/v1/completions -H "Content-Type: application/json" -d '{"model":"qwen","prompt":"who are you?","max_tokens":32}'

{"detail":"Service is not available"}
```

**定位思路/可能原因**

`controller_api_dns` 是 NodeManager / Coordinator 访问 Controller 使用的地址。K8s 部署时 Deployer 会注入实际 Service 名，一般无需在 user_config 里改。写成不存在的名字后，进程往往仍能起来，但调度侧拿不到可用 P/D 实例。`Service is not available` 表示 Coordinator 当前没有可用的 P/D 实例，不要只在 coordinator 日志里搜 ERROR。

```json
"motor_controller_config": {
  "api_config": {
    "controller_api_port": 1026,
    "controller_api_dns": "not-exist.invalid"
  }
}
```

**解决方案**

删除错误的 `controller_api_dns`（或改回集群里实际的 Controller Service 名），重新部署。

## 推理引擎部署失败

<a id="inference-engine-invalid-model-path"></a>

### **1、user_config 类：错误权重路径**

**问题现象**

user_config 中模型权重路径写错时，Deployer 仍能把 Pod 拉起来，失败出现在引擎节点 showlog。

例如正确路径为 `/correct_weight_path/ExampleModel-70B-Instruct`，误写成 `/wrong_weight_path/ExampleModel-70B-Instruct`。

showlog 中 P 节点打印：

```text
INFO 08-27 16:17:42 parsed arg_list: [..., '--model', '/wrong_weight_path/ExampleModel-70B-Instruct', ...]
ERROR:    Traceback (most recent call last):
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/transformers/utils/hub.py", line 422, in cached_files
    hf_hub_download(
  ...
huggingface_hub.errors.HFValidationError: Repo id must be in the form 'repo_name' or 'namespace/repo_name': '/wrong_weight_path/ExampleModel-70B-Instruct'. Use `repo_type` argument if needed.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  ...
  File "/usr/local/python3.11.10/lib/python3.11/site-packages/transformers/utils/hub.py", line 473, in cached_files
    raise OSError(f"{e}") from e
OSError: Repo id must be in the form 'repo_name' or 'namespace/repo_name': '/wrong_weight_path/ExampleModel-70B-Instruct'. Use `repo_type` argument if needed.

ERROR:    Application startup failed. Exiting.
(NodeManager pid=237) ERROR 08-27 16:19:26 [node_manager][heartbeat_manager.py:267] Failed to get engine server status from 192.168.1.12:10001: send request http://192.168.1.12:10001/status error: HTTPConnectionPool(host='192.168.1.12', port=10001): Max retries exceeded with url: /status (Caused by NewConnectionError("HTTPConnection(host='192.168.1.12', port=10001): Failed to establish a new connection: [Errno 111] Connection refused"))
(NodeManager pid=237) ERROR 08-27 16:19:41 [node_manager][heartbeat_manager.py:380] Reached 5 consecutive abnormal heartbeats, setting suicide flag for main to handle...
(NodeManager pid=237) INFO 08-27 16:19:47 [__main__][main.py:40] exit_code: -1
```

**定位思路/可能原因**

`model` 路径不是机器上实际存在的权重目录，vLLM 按 Hugging Face repo id 解析失败后退出，NodeManager 心跳失败后自杀。

**解决方案**

检查 user_config 中 prefill / decode 的 `model` 路径，改为机器上实际存在的权重目录。
