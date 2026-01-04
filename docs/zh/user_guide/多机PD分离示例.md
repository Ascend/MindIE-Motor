## 使用kubectl部署多机PD分离服务示例

### 限制与约束

- 该示例支持部署PD分离模式的Server、Coordinator、Controller。
- Atlas 800I A2 推理服务器需要配置卡IP并安装Ascend Operator组件。
- 当前部署脚本不支持NPU故障重调度场景。
- 异构场景中的P实例需部署在Atlas A2 推理系列产品（32G）设备上，D实例需部署在Atlas A2 推理系列产品（64G）设备上。

### 脚本介绍

本节使用MindIE Motor安装目录（examples/kubernetes_deploy_scripts）中的脚本实现一键式部署和卸载MindIE PD分离集群功能，集群管理员用户可参考这些脚本文件线下使用K8s kubectl工具操作集群。

集群管理员用户只需在管理节点完成启动脚本编写、业务配置和kubernetes配置，然后调用部署脚本，实现自动下发业务配置和启动脚本，自动全局ranktable生成，以及自动调度Pod到计算节点。

<div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
<strong>💡 说明</strong>
<ul style="margin:8px 0;padding-left:20px;">
    <li>MindIE的安装部署脚本需由K8s管理员执行，防止脚本或配置被恶意篡改，导致任意命令执行或容器逃逸风险。</li>
    <li>K8s管理员应严格管控MindIE ConfigMap的写、更新和删除权限，建议安装目录权限设置为750，文件权限为640。防止其被恶意修改后挂载至Pod内部引发安全风险，建议通过Namespace和RBAC方法进行权限约束。</li>
</ul></div>

脚本文件目录结构如下所示：

```
├── boot_helper
│   ├── boot.sh
│   ├── gen_config_single_container.py
│   ├── get_group_id.py
│   ├── mindie_cpu_binding.py
│   ├── server_prestop.sh
│   └── update_mindie_server_config.py
├── chat.sh
├── conf
├── delete.sh
├── deploy.sh
├── deploy_ac_job.py
├── deployment
│   ├── controller_init.yaml
│   ├── coordinator_init.yaml
│   ├── mindie_ms_controller.yaml
│   ├── mindie_ms_coordinator.yaml
│   ├── mindie_server.yaml
│   ├── mindie_server_heterogeneous.yaml
│   ├── mindie_service_single_container.yaml
│   ├── mindie_service_single_container_base_A3.yaml
│   ├── server_init.yaml
│   └── single_container_init.yaml
├── gen_ranktable_helper
│   ├── gen_global_ranktable.py
│   └── global_ranktable.json
├── generate_stream.sh
├── log.sh
├── user_config.json
├── user_config_base_A3.json
└── utils
    ├── validate_config.py
    └── validate_utils.py
```

**多机PD分离部署场景相关的关键目录及文件解释如下表所示：**

表1 关键目录及文件解释

| 目录/文件            | 说明                                                         |
| -------------------- | ------------------------------------------------------------ |
| conf                 | 集群管理组件和Server的主要业务配置文件，PD分离管理调度策略和模型相关配置。 |
| boot_helper          | 该目录包含容器启动脚本boot.sh，获取group_id，刷新环境变量到配置文件，设置启动程序的环境变量等，用户可根据需要在这里调整日志等级等。 |
| deployment           | K8s部署任务定义，配置NPU资源使用量，实例数，镜像等。         |
| gen_ranktable_helper | 生成global_ranktable.json的工具。                            |
| chat.sh              | 使用curl发送HTTPS请求给推理服务的简单对话示例，**适用于Prefix Cache场景**。 |
| generate_stream.sh   | 使用curl发送HTTPS请求给推理服务的流式响应示例。              |
| deploy.sh            | 部署入口脚本，一键拉起所有MindIE组件。                       |
| delete.sh            | 卸载脚本，一键卸载所有MindIE组件。                           |
| log.sh               | 查询Pod的打印日志，可查询到部署的所有Pod的日志。             |

<div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
<strong>💡 说明</strong>
<ul style="margin:8px 0;padding-left:20px;">
MindIE依赖jemalloc.so库文件，禁止在/usr/目录下安装非法同名的so文件，引入任意命令执行等安全风险。
</ul></div>

### 操作步骤

以LLaMA3-8B模型为例，每个实例配置2张卡，配置4个实例。部署样例如下所示，以下操作均在部署脚本路径下完成：

<div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
<strong>💡 说明</strong>
<ul style="margin:8px 0;padding-left:20px;">
以下操作步骤不区分同构部署和异构部署，异构部署在对应的操作步骤有单独的配置说明。
</ul></div>

1. 进入集群管理节点宿主机，首次部署用户需创建mindie命名空间，默认值为mindie，如需创建其它名称请自行替换。

   ```
   kubectl create namespace mindie
   ```

2. 配置Controller组件的启动配置文件ms_controller.json，其配置文件详细说明请参见[controller环境变量介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/controller.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E)。

   PD场景下需配置“deploy_mode”参数为PD分离部署模式，如下所示：

   ```
   "deploy_mode"= "pd_separate"
   ```

   如需进行异构部署，则需要配置“is_heterogeneous”参数为“true”，如下所示：

   ```
   is_heterogeneous: true
   ```

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       <li>ms_controller.json配置文件中的“request_coordinator_tls_enable”、“request_server_tls_enable”、“http_server_tls_enable”和“cluster_tls_enable”参数为控制是否使用HTTPS的开关。建议用户打开（即设置为“true”），确保通信安全；如果关闭则存在较高的网络安全风险。- true：集群内MindIE组件使用了HTTPS接口，并导入证书到容器内，配置相应的证书路径。- false：集群内MindIE组件使用了HTTP接口，无需准备证书文件。</li>
       <li>配置文件ms_controller.json中的“default_p_rate”和“default_d_rate”参数分别控制集群中P节点和D节点数量的比值（如设置环境变量MINDIE_MS_P_RATE和MINDIE_MS_D_RATE，则优先读取环境变量的值，环境变量详情请参见<a href="https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/controller.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E">controller环境变量介绍</a>)，默认均为0，根据模型、硬件、服务信息等自动决策最佳配比，也可以根据场景分别设置为P和D节点的实际数量。</li>
   </ul></div>

3. 配置Coordinator组件的启动配置文件ms_coordinator.json，其配置文件详细说明请参见[controller环境变量介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/controller.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E)。

   PD场景下需配置“deploy_mode”参数为PD分离部署模式，如下所示：

   ```
   deploy_mode="pd_separate"
   ```

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
   ms_coordinator.json配置文件中的
       “controller_server_tls_enable”、
       “request_server_tls_enable”、
       “mindie_client_tls_enable”和
       “mindie_mangment_tls_enable”
       参数为控制是否使用HTTPS的开关。建议用户打开（即设置为“true”），确保通信安全；如果关闭则存在较高的网络安全风险。
       <li>true：集群内MindIE组件使用了HTTPS接口，并导入证书到容器内，配置相应的证书路径。</li>
       <li>false：集群内MindIE组件使用了HTTP接口，无需准备证书文件。</li>
   </ul></div>

4. 配置Server服务启动的config.json配置文件，PD分离服务部署模式需要配置的参数如表2所示，具体参数解释请参见[Server环境变量介绍](https://gitcode.com/Ascend/MindIE-LLM/blob/br_develop_mindie/docs/zh/user_guide/user_manual/service_parameter_configuration.md)。

   表2 config.json关键配置参数解释

   | 参数            | 说明                                                         |
   | --------------- | ------------------------------------------------------------ |
   | modelName       | 模型名配置，关联模型权重文件的模型，如配置为llama3-8b。      |
   | modelWeightPath | 模型权重文件目录配置，默认情况下脚本会挂载物理机的/data目录，modelWeightPath需配置为/data路径下的模型权重路径，确保集群可调度Ascend计算节点在该路径下存在模型文件。 |
   | worldSize       | 配置一个P/D实例占用的NPU卡数；例如配置为“2”，表示使用两张卡。 |
   | npuDeviceIds    | 卡号配置成从0开始编号，总数与worldSize一致，如配置为[[0,1]]。 |
   | inferMode       | 配置为dmi。                                                  |
   | tp              | 整网张量并行数，取值为worldSize参数值；该参数为补充参数，请自行在ModelConfig字段下配置。 |

5. 配置http_client_ctl.json配置文件，该配置文件为集群启动、存活、就绪探针HTTP客户端工具的配置文件，具体参数解释请参见表4。

   “tls_enable”参数为控制是否使用HTTPS的开关，若集群内MindIE组件使用了HTTPS接口，需设置“tls_enable”为“true”，并导入证书到容器内，配置相应的证书路径。如使用HTTP接口，则设置“tls_enable”为“false”，无需准备证书文件。

   须知

   建议用户打开tls_enable，确保通信安全；如果关闭则存在较高的网络安全风险。

6. 配置kubernetes Deployment。

   在部署脚本目录中的deployment目录下找到mindie_server.yaml、mindie_ms_coordinator.yaml、mindie_ms_controller.yaml和mindie_server_heterogeneous.yaml（仅异构场景配置）文件。

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       <li>本脚本仅作为一个部署参考，Pod容器的安全性由用户自行保证，实际生产环境请针对镜像和Pod安全进行加固。</li>
       <li>用户在使用kubetl部署Deployment时，可修改deployment的配置yaml文件，请避免使用危险配置，确保使用安全镜像（非root权限用户），配置安全的Pod上下文。</li>
       <li>用户应挂载安全路径（非软链接，非系统危险路径，非业务敏感路径），并设置合理的文件目录权限，避免挂载/home等公共目录，防止被非法用户篡改，导致容器逃逸问题。</li>
   </ul></div>

   - mindie_server.yaml主要配置的字段如下所示：

     - replicas：

       - 同构场景：配置P和D的总实例数，如果配置为3P1D，即配置为4。
       - 异构场景：配置P的实例数，如果配置为3P，即配置为3。

     - huawei.com/Ascend910：配置一个P/D实例占用的NPU卡数，与MindIE Serve的config.json配置文件中worldSize参数配置的卡数保持一致。

     - startupProbe：启动探针，默认启动时间为500秒，如果在该时间内服务未启动成功，Pod将会自动重启。请用户根据实际场景设置合理的启动时间。

     - affinity：反亲和部署配置，默认注释不启用，去掉注释后开启。开启后，每个Server Pod将会反亲和部署到不同的计算节点，多个Pod不会部署到同一节点。
     
     - nodeSelector：节点选择，配置期望调度的节点，通过节点参数实现。
     
       - hardware_type：异构部署需要配置，同构部署无需配置；使能Controller决策异构推理身份。
     
       - hardware-type：异构部署需要配置，同构部署无需配置；是纳管到K8s集群的节点标签，用于分配异构设备资源，异构部署之前，需执行下面命令为异构计算节点打上标签。
     
         ```
         kubectl label node xx_node hardware-type=xx_device
         ```
     
         <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
         <strong>💡 说明</strong>
         <ul style="margin:8px 0;padding-left:20px;">
             异构场景下，参数取值需满足以下要求：
             <li>hardware_type：如配置为P节点，该参数必须为"800I A2(32G)"；如需配置为D节点，该参数必须为"800I A2(64G)"。</li>
             <li>hardware-type：该参数的值不能有空格。</li>    
         </ul></div>
     
     - MINDIE_LOG_TO_FILE：统一设置MindIE各组件日志是否写入文件。
     
       默认值为1，日志写入文件。取值范围为：[false, true]，且支持[0, 1]。
     
     - MINDIE_LOG_TO_STDOUT：统一设置MindIE各组件是否打印日志。
     
       默认值为1，打印日志。取值范围为：[false, true]，且支持[0, 1]。
     
     - MINDIE_LOG_LEVEL：统一设置MindIE各组件日志级别。
     
       默认值为INFO。日志级别取值 [CRITICAL, ERROR, WARN, INFO, DEBUG]。
     
   - mindie_ms_coordinator.yaml和mindie_ms_controller.yaml文件主要关注image镜像名的修改及日志设置。
   
     - MINDIE_LOG_TO_FILE：统一设置MindIE各组件日志是否写入文件。
   
       默认值为1，日志写入文件。取值范围为：[false, true]，且支持[0, 1]。
   
     - MINDIE_LOG_TO_STDOUT：统一设置MindIE各组件是否打印日志。
   
       默认值为1，打印日志。取值范围为：[false, true]，且支持[0, 1]。
   
     - MINDIE_LOG_LEVEL：统一设置MindIE各组件日志级别。
   
       默认值为INFO。日志级别取值 [CRITICAL, ERROR, WARN, INFO, DEBUG]。
   
     <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
     <strong>💡 说明</strong>
     <ul style="margin:8px 0;padding-left:20px;">
         <li>mindie_ms_coordinator.yaml文件需特别关注livenessProbe（存活探针）参数，在高并发推理请求场景下，可能会因为探针超时，从而被K8s识别为Pod不存活，导致K8s重启Coordinator容器，建议用户谨慎开启livenessProbe（存活探针）。</li>
         <li>为了支持MindIE Controller故障恢复功能，mindie_ms_controller.yaml文件中volumes参数的name: status-data所配置的目录，必须为待部署计算节点（该计算节点通过nodeSelector参数指定）上真实存在，且volumeMounts参数中的name: status-data挂载路径必须为：/*MindIE Motor安装路径*/logs。</li>    
     </ul></div>
     
   - mindie_server_heterogeneous.yaml文件主要配置的字段如下所示：

     - replicas：配置D的实例数，如果配置为4D，即配置为4。
   
     - huawei.com/Ascend910：配置一个P/D实例占用的NPU卡数，2卡即配置为2。
   
     - startupProbe：启动探针，默认启动时间为500秒，如果在该时间内服务未启动成功，Pod将会自动重启。请用户根据实际场景设置合理的启动时间。
   
     - nodeSelector：节点选择，配置期望调度的节点，通过节点参数实现。
     
     - hardware_type：异构部署需要配置，同构部署无需配置；使能Controller决策异构推理身份，该参数配置的值与mindie_server.yaml文件中hardware_type参数的值不能一样。
     
     - hardware-type：异构部署需要配置，同构部署无需配置；是纳管到K8s集群的节点标签，用于分配异构设备资源，异构部署之前，需执行下面命令为异构计算节点打上标签，该参数配置的值与mindie_server.yaml文件中hardware-type参数的值不能一样。
     
       ```
         kubectl label node xx_node hardware-type=xx_device2
       ```
     
       <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
         <strong>💡 说明</strong>
         <ul style="margin:8px 0;padding-left:20px;">
             异构场景下，参数取值需满足以下要求：
           <li>hardware_type：如配置为P节点，该参数必须为"800I A2(32G)"；如需配置为D节点，该参数必须为"800I A2(64G)"。</li>
             <li>hardware-type：该参数的值不能有空格。</li>    
         </ul></div>
     
     - MINDIE_LOG_TO_FILE：统一设置MindIE各组件日志是否写入文件。
     
       默认值为1，日志写入文件。取值范围为：[false, true]，且支持[0, 1]。
     
   - MINDIE_LOG_TO_STDOUT：统一设置MindIE各组件是否打印日志。
     
     默认值为1，打印日志。取值范围为：[false, true]，且支持[0, 1]。
     
   - MINDIE_LOG_LEVEL：统一设置MindIE各组件日志级别。
     
     默认值为INFO。日志级别取值 [CRITICAL, ERROR, WARN, INFO, DEBUG]。
   
7. 配置启动脚本boot.sh，可配置环境变量请参考表3

   表3 环境变量列表

   | 环境变量                                                     | 类型     | 说明                                                         |
   | ------------------------------------------------------------ | -------- | ------------------------------------------------------------ |
   | MINDIE_INFER_MODE                                            | PD分离   | 推理模式，表示是否PD分离。standard：PD混部；dmi：PD分离。该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_DECODE_BATCH_SIZE                                     | 公共变量 | 最大Decode的batch大小。取值范围：[1, 5000]该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_PREFILL_BATCH_SIZE                                    | 公共变量 | 最大Prefill的batch大小。取值范围：[1, MINDIE_DECODE_BATCH_SIZE - 1]该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_MAX_SEQ_LEN                                           | 公共变量 | 最大序列长度。整型数字，取值范围：(0, 4294967295]该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_MAX_ITER_TIMES                                        | 公共变量 | 最大输出长度。整型数字，取值范围：[1, MINDIE_MAX_SEQ_LEN-1]该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_MODEL_NAME                                            | 公共变量 | 模型名。该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_MODEL_WEIGHT_PATH                                     | 公共变量 | 模型权重文件路径。该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_ENDPOINT_HTTPS_ENABLED                                | 公共变量 | 是否在Prefill/Decode实例上启用HTTPS。true：启用；false：禁用。该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | MINDIE_INTER_COMM_TLS_ENABLED                                | 公共变量 | 推理实例间通信开启TLS。true：启用；false：禁用。该环境变量未在boot.sh脚本默认配置中，如有需要可自行添加。 |
   | HCCL_RDMA_RETRY_CNT                                          | 公共变量 | 用于配置RDMA网卡的重传次数，需要配置为整数，取值范围为[1,7]，默认值为7。 |
   | HCCL_RDMA_TIMEOUT                                            | 公共变量 | 用于配置RDMA网卡重传超时时间的系数timeout。RDMA网卡重传超时时间最小值的计算公式为：4.096 μs * 2 ^ timeout，其中timeout为该环境变量配置值，且实际重传超时时间与用户网络状况有关。该环境变量配置为整数，取值范围为[5,20]，默认值为18。 |
   | HCCL_EXEC_TIMEOUT                                            | 公共变量 | 通过该环境变量可控制设备间执行时同步等待的时间，在该配置时间内各设备进程等待其他设备执行通信同步。此处用于设置首token超时时间。单位为s，取值范围为：[0, 2147483647]，默认值为60，当配置为0时代表永不超时。 |
   | HSECEASY_PATH                                                | 公共变量 | KMC解密工具的依赖库路径.                                     |
   | MINDIE_MS_CONTROLLER_CONFIG_FILE_PATH                        | 公共变量 | Controller组件配置文件路径。                                 |
   | MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH                       | 公共变量 | Coordinator组件配置文件路径。                                |
   | **注：日志相关环境变量****详情请参见[日志环境变量介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/log_configuration.md) |          |                                                              |

8. 拉起PD集群。

   配置容器内mindie安装的目录：根据制作镜像时实际的安装路径，修改MINDIE_USER_HOME_PATH的value值，如安装路径是/xxx/Ascend/mindie，则配置为/xxx 。

   ```
   export MINDIE_USER_HOME_PATH={镜像的安装路径}
   ```

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       拉起服务前，建议用户使用MindStudio的预检工具进行配置文件字段校验，辅助校验配置的合法性，详情请参见[链接](https://gitcode.com/Ascend/msit/tree/master/msprechecker)。    
   </ul></div>

   使用以下命令拉起集群。

   ```
   bash deploy.sh
   ```

   执行命令后，会同步等待global_ranktable.json生成完成，如长时间处于阻塞状态，请ctrl+c中断后查看集群Pod状态，进行下一步的调试定位。

   global_ranktable.json样例如下所示，样例中参数解释如表4所示。

   ```
   {
     "version": "1.0",
     "server_group_list": [
       {
         "group_id": "2",
         "server_count": "2",
         "server_list": [
           {
             "server_id": "xxx.xxx.xxx.1",
             "server_ip": "xxx.xxx.xxx.1",
             "device": [
               {
                 "device_id": "0",
                 "device_ip": "xxx.xxx.xxx.1",
                 "device_logical_id": "0"
               }
             ],
             "hardware_type": "800I A2(32G)"
           },
           {
             "server_id": "xxx.xxx.xxx.2",
             "server_ip": "xxx.xxx.xxx.2",
             "device": [
               {
                 "device_id": "1",
                 "device_ip": "xxx.xxx.xxx.2",
                 "device_logical_id": "1"
               }
             ],
             "hardware_type": "800I A2(64G)"
           }
         ]
       },
       {
         "group_id": "1",
         "server_count": "1",
         "server_list": [
           {
             "server_id": "xxx.xxx.xxx.1",
             "server_ip": "xxx.xxx.xxx.1"
           }
         ]
       },
       {
         "group_id": "0",
         "server_count": "1",
         "server_list": [
           {
             "server_id": "xxx.xxx.xxx.1",
             "server_ip": "xxx.xxx.xxx.1"
           }
         ]
       }
     ],
     "status": "completed"
   }
   ```

   表4 global_ranktable.json文件参数解释

   | 参数              | 类型         | 描述                                                         |
   | ----------------- | ------------ | ------------------------------------------------------------ |
   | version           | string       | Ascend Operator的版本号。                                    |
   | status            | string       | 集群信息表的状态。completed：部署完成。initializing：初始化中。 |
   | group_id          | string       | 各组件的ID。0：存储Coordinator的部署信息。1：存储Controller的部署信息。2：存储Server的部署信息。 |
   | server_count      | string       | 各组件的节点总数。                                           |
   | server_list       | json对象数组 | 各组件的节点部署信息。最多包含1个Controller实例，列表有效长度[0, 1]。最多包含1个Coordinator实例，列表有效长度[0, 1]。最多包含96个Server实例，列表有效长度[0, 96]。 |
   | server_id         | string       | 组件节点的主机IP。                                           |
   | server_ip         | string       | 组件节点的IP地址。                                           |
   | device            | json对象数组 | NPU设备信息，仅Server有此属性。列表有效长度[1, 128]。        |
   | device_id         | string       | NPU的设备ID。                                                |
   | device_ip         | string       | NPU的IP地址。                                                |
   | device_logical_id | string       | NPU的逻辑ID，即Server所在Pod内可见的卡设备的序列ID。         |
   | hardware_type     | string       | 硬件设备类型，仅异构模式下有此属性。如配置为P节点，该参数为"800I A2(32G)"；如需配置为D节点，该参数为"800I A2(64G)"。 |

   说明

   - 如果部署失败，则需要参见11卸载集群后重新部署。

   - 集群默认刷新挂载到容器内的configmap的频率是60s，如遇到容器内打印“status of ranktable is not completed”日志信息的时间偏久，可在每个待调度的计算节点修改kubelet同步configmap的周期，即修改/var/lib/kubelet/config.yaml中的syncFrequency参数，将周期减少到5s，注意此修改可能影响集群性能。

     ```
     syncFrequency: 5s
     ```

     然后使用以下命令重启kubelet：

     ```
     swapoff -a
     systemctl restart  kubelet.service
     systemctl status kubelet
     ```

   - 确保Docker配置了标准输出流写入到文件的最大规格，防止磁盘占满导致Pod被驱逐。

     需在待部署服务的计算节点上修改Docker配置文件后重启Docker：

     1. 使用以下命令打开daemon.json文件。

        ```
        vim /etc/docker/daemon.json
        ```

        在daemon.json文件中添加日志选项“log-opts”，具体内容如下所示。

        ```
        "log-opts":{"max-size":"500m", "max-file":"3"}
        ```

        参数解释：

        max-size=500m：表示一个容器日志大小上限是500M。

        max-file=3：表示一个容器最多有三个日志，超过会自动滚动更新。

     2. 使用以下命令重启Docker.

        ```
        systemctl daemon-reload
        systemctl restart docker
        ```

   若使能异构集推理，执行以下命令：

   ```
   bash deploy.sh heter
   ```

9. 使用kubectl命令查看PD集群状态。

   ```
   kubectl get pods -n mindie
   ```

   如启动4个Server实例，回显如下所示：

   ```
   NAME                                     READY   STATUS    RESTARTS   AGE    IP               NODE       NOMINATED NODE   READINESS GATES
   mindie-ms-controller-7845dcd697-h4gw7    1/1     Running   0          145m   xx.xx.xx.xx    ubuntu10   <none>           <none>
   mindie-ms-coordinator-6bff995ff8-l6fwz   1/1     Running   0          145m   xx.xx.xx.xx    ubuntu10   <none>           <none>
   mindie-server-7b795f8df9-2xvh4           1/1     Running   0          145m   xx.xx.xx.xx   ubuntu     <none>           <none>
   mindie-server-7b795f8df9-j4z7d           1/1     Running   0          145m   xx.xx.xx.xx   ubuntu     <none>           <none>
   mindie-server-7b795f8df9-v2tcz           1/1     Running   0          145m   xx.xx.xx.xx   ubuntu     <none>           <none>
   mindie-server-7b795f8df9-vl9hv           1/1     Running   0          145m   xx.xx.xx.xx   ubuntu     <none>           <none>
   ```

   - 以mindie-ms-controller命名开头为Controller控制器组件。
   - 以mindie-ms-coordinator命名开头为Coordinator调度器组件。
   - 以mindie-server命名开头为Server推理服务组件。

   如观察Pod进入Running状态，表示Pod容器已成功被调度到节点并正常启动，但还需要进一步确认业务程序是否启动成功。

   - 通过脚本示例提供的log.sh脚本可查询这些Pod的标准输出日志，查看程序是否出现异常：

     ```
     bash log.sh
     ```

     <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
     <strong>💡 说明</strong>
     <ul style="margin:8px 0;padding-left:20px;">
         若使配置了异构集群推理，执行以下命令：
         bash log.sh heter
     </ul></div>

   - 如需要查询具体某个Pod（如上面mindie-server-7b795f8df9-vl9hv）的日志，则执行以下命令：

     ```
     kubectl logs mindie-server-7b795f8df9-vl9hv -n mindie
     ```

   - 如需要进入容器查找更多定位信息，则执行以下命令：

     ```
     kubectl exec -it mindie-server-7b795f8df9-vl9hv -n mindie -- bash
     ```

   - 如需确认的P，D对应的Pod，待Controller组件启动（如上面mindie-ms-controller-7845dcd697-h4gw7处于READY 1/1状态）后，则执行以下命令：

     ```
     kubectl logs mindie-ms-controller-7845dcd697-h4gw7 -n mindie | grep UpdateServerInfo
     ```

     查询到P节点和D节点的Pod IP，并结合上面查询Pod状态命令回显的IP可找到对应的Pod。

10. 通过脚本示例提供的generate_stream.sh发起流式推理请求。

    部署成功后，会在节点上开放31015端口用于推理业务接口，需要修改generate_stream.sh中的IP地址为集群管理节点宿主机的IP地址，如Coordinator组件启用了HTTPS，需要配置相关的证书；如使用HTTP，需修改脚本中的HTTPS为HTTP，并删除证书相关配置。

    ```
    bash generate_stream.sh
    ```

11. 卸载PD集群。

    如需停止PD服务或者修改业务配置重新部署实例，需要调用以下命令卸载已部署的实例，重新部署请执行8。

    ```
    bash delete.sh mindie ./
    ```

    <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
    <strong>💡 说明</strong>
    <ul style="margin:8px 0;padding-left:20px;">
        <li>mindie为1创建的命名空间，请根据实际的命名空间进行替换。</li>
        <li>delet.sh卸载脚本需要在examples/kubernetes_deploy_scripts目录下执行，否则无法停止服务并报错。</li>
    </ul></div>