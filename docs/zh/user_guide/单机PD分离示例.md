## 使用kubectl部署单机PD分离服务示例

### 限制与约束

- 该示例支持部署PD分离模式的Server、Coordinator和Controller。
- Atlas 800I A2 推理服务器需要配置卡IP并安装Ascend Operator组件。
- 当前部署脚本不支持NPU故障重调度场景。

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

**单机PD分离部署场景相关的关键目录及文件解释如下表所示：**

表1 关键目录及文件解释

| 目录/文件          | 说明                                                         |
| ------------------ | ------------------------------------------------------------ |
| conf               | 集群管理组件和Server的主要业务配置文件，PD分离管理调度策略和模型相关配置。 |
| boot_helper        | 该目录包含容器启动脚本boot.sh，对conf目录下配置文件修正（包括在用户只提供一份Server的config.json情况下生成多份端口号不冲突的config文件）及生成对应global_ranktable的辅助脚本gen_config_single_container.py。 |
| deployment         | K8s部署任务定义，配置NPU资源使用量，实例数，镜像等。在单机PD分离部署场景下只使用到mindie_service_single_container.yaml文件。 |
| chat.sh            | 使用curl发送HTTPS请求给推理服务的简单对话示例，**适用于Prefix Cache场景**。 |
| generate_stream.sh | 使用curl发送HTTPS请求给推理服务的流式响应示例。              |
| deploy.sh          | 部署入口脚本，一键拉起所有MindIE组件。                       |
| delete.sh          | 卸载脚本，一键卸载所有MindIE组件。                           |
| log.sh             | 查询Pod的打印日志，可查询到部署的所有Pod的日志。             |

<div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
<strong>💡 说明</strong>
<ul style="margin:8px 0;padding-left:20px;">
    <li>说明MindIE依赖jemalloc.so库文件，禁止在/usr/目录下安装非法同名的so文件，引入任意命令执行等安全风险。</li>
</ul></div>


#### 操作步骤

以LLaMA3-8B模型为例，每个实例配置2张卡，配置4个实例。部署样例如下所示，以下操作均在部署脚本路径下完成：

1. 进入集群管理节点宿主机，首次部署用户需创建命名空间，默认值为mindie，如需创建其它名称请自行替换。

   ```
   kubectl create namespace mindie
   ```

2. 配置Controller组件的启动配置文件ms_controller.json，其配置文件详细说明请参见[controller环境变量介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/controller.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E)。

   配置“deploy_mode”参数为“pd_disaggregation_single_container”，表示单机PD分离部署模式，如下所示：

   ```
   "deploy_mode"= "pd_disaggregation_single_container"
   ```

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       <li>ms_controller.json配置文件中的“request_coordinator_tls_enable”、“request_server_tls_enable”、“http_server_tls_enable”和“cluster_tls_enable”参数为控制是否使用HTTPS的开关。建议用户打开（即设置为"true"），确保通信安全；如果关闭则存在较高的网络安全风险。true：集群内MindIE组件使用了HTTPS接口，并导入证书到容器内，配置相应的证书路径。false：集群内MindIE组件使用了HTTP接口，无需准备证书文件。</li>
       <li>由于单机PD分离部署功能中所有组件处于同一个Pod，文件中"http_server"模块的port默认值为1026，与其他端口号存在冲突，则需修改为其他非冲突端口号，建议值为1027。（配置文件中配置的端口存在冲突情况下，程序将自动分配其他非冲突端口号保证程序能正常拉起）</li>
       <li>配置文件ms_controller.json中的"default_p_rate"和"default_d_rate"参数分别控制集群中P节点和D节点数量的比值（如设置环境变量MINDIE_MS_P_RATE和MINDIE_MS_D_RATE，则优先读取环境变量的值，环境变量详情请参见<a href="https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/controller.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E">controller环境变量介绍</a>），默认均为0，根据模型、硬件、服务信息等自动决策最佳配比，也可以根据场景分别设置为P和D节点的实际数量。</li>
   </ul></div>

3. 配置Coordinator组件的启动配置文件ms_coordinator.json，其配置文件详细说明请参见[Coordinator环境变量介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/coordinator.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E)。

   PD场景下需配置“deploy_mode”参数为单机PD分离部署模式，如下所示：

   ```
   deploy_mode="pd_disaggregation_single_container"
   ```

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       ms_coordinator.json配置文件中的“controller_server_tls_enable”、“request_server_tls_enable”、“mindie_client_tls_enable”和 “mindie_mangment_tls_enable”参数为控制是否使用HTTPS的开关。建议用户打开（即设置为“true”），确保通信安全；如果关闭则存在较高的网络安全风险。
       <li>true：集群内MindIE组件使用了HTTPS接口，并导入证书到容器内，配置相应的证书路径。</li>
       <li>false：集群内MindIE组件使用了HTTP接口，无需准备证书文件。</li>
   </ul></div>

4. 配置Server服务启动的config.json配置文件，PD分离服务部署模式需要配置的参数如表2所示，具体参数解释请参见[Server环境变量介绍](https://gitcode.com/Ascend/MindIE-LLM/blob/br_develop_mindie/docs/zh/user_guide/user_manual/service_parameter_configuration.md)。

   表2 config.json关键配置参数解释

   | 参数            | 说明                                                         |
   | --------------- | ------------------------------------------------------------ |
   | modelName       | 模型名配置，关联模型权重文件的模型，如配置为llama3-8b。      |
   | modelWeightPath | 模型权重文件目录配置，需配置为mindie_service_single_container.yaml文件中指定挂载在容器中的权重路径（默认为/mnt/mindie-service/ms/model），确保集群可调度Ascend计算节点在该路径下存在模型文件。 |
   | worldSize       | 配置一个P/D实例占用的NPU卡数；例如配置为“2”，表示使用两张卡。 |
   | npuDeviceIds    | 卡号配置成从0开始编号，总数与worldSize一致，如配置为[[0,1]]。 |
   | inferMode       | 配置为dmi。                                                  |
   | tp              | 整网张量并行数，取值为worldSize参数值；该参数为补充参数，请自行在ModelConfig字段下配置。 |

   在单机PD分离部署场景下，多个Server进程将运行于同一Pod中，每个Server进程需对应独立的配置文件。为简化配置流程，可使用mindie_service_single_container.yaml文件中的MINDIE_MS_GEN_SERVER_PORT环境变量进行管理。该环境变量支持两种配置模式：

   - true（默认值）：系统将根据用户配置的config.json文件，自动生成多个配置文件（config1.json、config2.json、...、config{server_num}.json）。每个配置文件对应一个Server进程，系统将为配置文件中的端口参数：port、managementPort、metricsPort和interCommPort分配互不冲突的端口号，确保各进程独立运行。
   - false：用户需提供与Server数量匹配的配置文件（config1.json、config2.json、...、config{server_num}.json）。在该模式下，用户可自由配置各端口参数。系统具备端口冲突检测机制，若配置文件中存在端口冲突，系统将自动分配其他可用端口号，确保程序正常启动。

5. 配置http_client_ctl.json配置文件，该配置文件为集群启动、存活、就绪探针HTTP客户端工具的配置文件，具体参数解释请参见表4。

   “tls_enable”参数为控制是否使用HTTPS的开关，若集群内MindIE组件使用了HTTPS接口，需设置“tls_enable”为“true”，并导入证书到容器内，配置相应的证书路径。如使用HTTP接口，则设置“tls_enable”为“false”，无需准备证书文件。

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>📌 须知</strong>
   <ul style="margin:8px 0;padding-left:20px;">
   建议用户打开tls_enable，确保通信安全；如果关闭则存在较高的网络安全风险。
   </ul></div>

6. 配置kubernetes Deployment，主要配置参数如下所示。

   Atlas 800I A2 推理服务器：在部署脚本目录中配置deployment路径下的mindie_service_single_container.yaml文件。

   Atlas 800I A3 超节点服务器：在部署脚本目录中配置deployment路径下的mindie_service_single_container_base_A3.yaml文件。

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       ms_coordinator.json配置文件中的“controller_server_tls_enable”、“request_server_tls_enable”、“mindie_client_tls_enable”和 “mindie_mangment_tls_enable”参数为控制是否使用HTTPS的开关。建议用户打开（即设置为“true”），确保通信安全；如果关闭则存在较高的网络安全风险。
       <li>本脚本仅作为一个部署参考，Pod容器的安全性由用户自行保证，实际生产环境请针对镜像和Pod安全进行加固。</li>
       <li>用户在使用kubetl部署Deployment时，可修改deployment的配置yaml文件，请避免使用危险配置，确保使用安全镜像（非root权限用户），配置安全的Pod上下文。</li>
       <li>用户应挂载安全路径（非软链接，非系统危险路径，非业务敏感路径），并设置合理的文件目录权限，避免挂载/home等公共目录，防止被非法用户篡改，导致容器逃逸问题。</li>
       <li>宿主机模型权重挂载路径需要根据实际情况配置，示例如下：</li>
       - name: model-path
     hostPath:
       path: /data/LLaMA3-8B
   </ul></div>


   - mindie_service_single_container.yaml主要配置的字段如下所示：

     - huawei.com/Ascend910：配置所有P/D实例占用的NPU卡数总和，与Server的所有config配置文件中worldSize参数配置的卡数总和保持一致。

     - sp-block：super-pod块大小，指代虚拟超节点的NPU数量。该参数仅在使用Atlas 800I A3 超节点服务器时配置（即的mindie_service_single_container_base_A3.yaml文件），其值与huawei.com/Ascend910参数的值保持一致。

     - startupProbe：启动探针，每180秒检测一次启动状态，如果启动探针连续失败的次数达到30次则认为未启动成功，Pod将会自动重启。请用户根据实际场景设置合理的启动时间。

     - readinessProbe：就绪探针，每180秒检测一次就绪状态。如果探针失败，Pod将停止接收流量，直到再次通过检查。请用户根据实际场景设置合理的触发时间。

   - livenessProbe：存活探针，每180秒检测一次存活状态。用于探测容器的健康，如果任意进程无应答则执行重启等操作。请用户根据实际场景设置合理的触发时间。

   - MINDIE_MS_GEN_SERVER_PORT：判定Server的多份配置文件是否由程序根据基本config.json文件自动生成。

   - MINDIE_MS_P_RATE：PD分离部署模式下，P所占的比例。

     - 0：表示自动决策最佳比例，MINDIE_MS_D_RATE需要同时为0；

       - 非0：表示指定P的比例，MINDIE_MS_D_RATE需要同时非0，且MINDIE_MS_P_RATE和MINDIE_MS_D_RATE的和小于等于单机NPU卡数。

       该环境变量的优先级高于ms_controller.json配置文件中default_p_rate。

     - MINDIE_MS_D_RATE：PD分离部署模式下，D所占的比例。

     - 0：表示自动决策最佳比例，MINDIE_MS_P_RATE需要同时为0；

       - 非0：表示指定P的比例，MINDIE_MS_P_RATE需要同时非0，且MINDIE_MS_P_RATE和MINDIE_MS_D_RATE的和小于等于单机NPU卡数。

       优先级高于ms_controller.json配置文件中default_d_rate。

     - MINDIE_LOG_TO_FILE：统一设置MindIE各组件日志是否写入文件。

     默认值为1，表示写入文件。取值范围为：[false, true]，且支持[0, 1]。

   - MINDIE_LOG_TO_STDOUT：统一设置MindIE各组件日志是否打印。

     默认值为1，表示打印日志。取值范围为：[false, true]，且支持[0, 1]。

   - MINDIE_LOG_LEVEL：统一设置MindIE各组件日志级别。

     默认值为INFO。日志级别取值 [CRITICAL, ERROR, WARN, INFO, DEBUG]。

7. 配置启动脚本boot.sh，可配置环境变量请参考表3。

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
   | HSECEASY_PATH                                                | 公共变量 | KMC解密工具的依赖库路径。                                    |
   | MINDIE_MS_CONTROLLER_CONFIG_FILE_PATH                        | 公共变量 | Controller组件配置文件路径。                                 |
   | MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH                       | 公共变量 | Coordinator组件配置文件路径。                                |
   | ATB_LLM_HCCL_ENABLE                                          | 公共变量 | 是否使能HCCL通信后端，默认开启。1：启用；0：禁用。使用Atlas 800I A2 推理服务器部署单机PD分离服务运行稠密模型时，为获得最优性能，建议关闭该环境变量。 |
   | HCCL_OP_EXPANSION_MODE                                       | 公共变量 | 用于配置通信算法的编排展开位置，默认值为：AIV。当ATB_LLM_HCCL_ENABLE配置为1时，该环境变量生效，取值如下：AI_CPU：代表通信算法的编排展开位置在Device侧的AI CPU计算单元。AIV：代表通信算法的编排展开位置在Device侧的Vector Core计算单元。HOST：代表通信算法的编排展开位置为Host侧CPU，Device侧根据硬件型号自动选择相应的调度器。HOST_TS：代表通信算法的编排展开位置为Host侧CPU，Host向Device的Task Scheduler下发任务，Device的Task Scheduler负责任务的调度与执行。该环境变量详情请参见CANN 环境变量介绍。 |
   | HCCL_RDMA_RETRY_CNT                                          | 公共变量 | 用于配置RDMA网卡的重传次数，需要配置为整数，取值范围为[1,7]，默认值为7。 |
   | HCCL_RDMA_TIMEOUT                                            | 公共变量 | 用于配置RDMA网卡重传超时时间的系数timeout。RDMA网卡重传超时时间最小值的计算公式为：4.096 μs * 2 ^ timeout，其中timeout为该环境变量配置值，且实际重传超时时间与用户网络状况有关。该环境变量配置为整数，取值范围为[5,20]，默认值为18。 |
   | HCCL_EXEC_TIMEOUT                                            | 公共变量 | 通过该环境变量可控制设备间执行时同步等待的时间，在该配置时间内各设备进程等待其他设备执行通信同步。此处用于设置首token超时时间。单位为s，取值范围为：[0, 2147483647]，默认值为60，当配置为0时代表永不超时。 |
   | **注：日志相关环境变量****详情请参见[日志环境变量介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/log_configuration.md) |          |                                                              |

8. 拉起单机PD分离服务。

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       拉起服务前，建议用户使用MindStudio的预检工具进行配置文件字段校验，辅助校验配置的合法性，详情请参见<a href="https://gitcode.com/Ascend/msit/tree/master/msprechecker">链接</a>。
       </ul></div>


   配置容器内MindIE安装的目录：根据制作镜像时实际的安装路径，修改MINDIE_USER_HOME_PATH的value值，如安装路径是/xxx/Ascend/mindie，则配置为/xxx 。

   ```
export MINDIE_USER_HOME_PATH={镜像的安装路径}
   ```

   - Atlas 800I A2 推理服务器

     使用以下命令拉起集群：

     ```
     bash deploy.sh
     ```

     或

     ```
     bash deploy.sh 800i_a2
     ```

   - Atlas 800I A3 超节点服务器

     使用以下命令可拉起集群：

     ```
     bash deploy.sh 800i_a3
     ```

9. 使用kubectl命令查看PD集群状态。

   ```
   kubectl get pods -n mindie
   ```

   回显示例如下：

   ```
   NAME                                     READY   STATUS    RESTARTS   AGE    IP               NODE       NOMINATED NODE   READINESS GATES
   mindie-server-7b795f8df9-vl9hv           1/1     Running   0          145m   xx.xx.xx.xx   ubuntu     <none>           <none>
   ```

   - Controller、Coordinator和Server组件均启动在以mindie-server开头的Pod中。

   如观察Pod进入Running状态，表示Pod容器已成功被调度到节点并正常启动，但还需要进一步确认业务程序是否启动成功。

   - 通过脚本示例提供的log.sh脚本可查询这些Pod的标准输出日志，查看程序是否出现异常：

     ```
     bash log.sh
     ```

   - 如需要查询具体Pod（如上面mindie-server-7b795f8df9-vl9hv）的日志，则执行以下命令：

     ```
     kubectl logs mindie-server-7b795f8df9-vl9hv -n mindie
     ```

   - 获取的Pod日志将输出生成global_ranktable.json文件。

     - Atlas 800I A2 推理服务器

       生成global_ranktable.json文件如下所示，样例中参数解释如

       表4

       所示。

       ```
       {
         "version": "1.0",
         "server_group_list": [
           {
             "group_id": "2",
             "server_count": "4",
             "server_list": [
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "0",
                      "device_ip": "1.1.1.1",
                      "rank_id": "0",
                      "device_logical_id": "0"
                    },
                   {
                      "device_id": "1",
                      "device_ip": "1.1.1.2",
                      "rank_id": "1",
                      "device_logical_id": "1"
                    }
                 ]
               },
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "2",
                      "device_ip": "1.1.1.3",
                      "rank_id": "2",
                      "device_logical_id": "2"
                    },
                   {
                      "device_id": "3",
                      "device_ip": "1.1.1.4",
                      "rank_id": "3",
                      "device_logical_id": "3"
                    }
                 ]
               },
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "4",
                      "device_ip": "1.1.1.5",
                      "rank_id": "4",
                      "device_logical_id": "4"
                    },
                   {
                      "device_id": "5",
                      "device_ip": "1.1.1.6",
                      "rank_id": "5",
                      "device_logical_id": "5"
                    }
                 ]
               },
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "6",
                      "device_ip": "1.1.1.7",
                      "rank_id": "6",
                      "device_logical_id": "6"
                    },
                   {
                      "device_id": "7",
                      "device_ip": "1.1.1.8",
                      "rank_id": "7",
                      "device_logical_id": "7"
                    }
                 ]
               }
             ]
           },
           {
             "group_id": "1",
             "server_count": "1",
             "server_list": [
               {
                 "server_ip": "127.0.0.1"
               }
             ]
           },
           {
             "group_id": "0",
             "server_count": "1",
             "server_list": [
               {
                 "server_ip": "127.0.0.1"
               },
             ]
           }
         ],
         "status": "completed"
       }
       ```

     - Atlas 800I A3 超节点服务器

       生成global_ranktable.json文件如下所示，样例中参数解释如表4所示。

       ```
       {
         "version": "1.0",
         "server_group_list": [
           {
             "group_id": "2",
             "server_count": "4",
             "server_list": [
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "0",
                      "device_ip": "1.1.1.1",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "0",
                      "device_logical_id": "0"
                    },
                   {
                      "device_id": "1",
                      "device_ip": "1.1.1.2",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "1",
                      "device_logical_id": "1"
                    }
                 ]
               },
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "2",
                      "device_ip": "1.1.1.3",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "2",
                      "device_logical_id": "2"
                    },
                   {
                      "device_id": "3",
                      "device_ip": "1.1.1.4",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "3",
                      "device_logical_id": "3"
                    }
                 ]
               },
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "4",
                      "device_ip": "1.1.1.5",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "4",
                      "device_logical_id": "4"
                    },
                   {
                      "device_id": "5",
                      "device_ip": "1.1.1.6",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "5",
                      "device_logical_id": "5"
                    }
                 ]
               },
               {
                 "server_id": "127.0.0.1",
                 "server_ip": "127.0.0.1",
                 "predict_port": "xxxx",
                 "mgmt_port": "xxxx",
                 "metric_port": "xxxx",
                 "inter_comm_port": "xxxx",
                 "device": [
                   {
                      "device_id": "6",
                      "device_ip": "1.1.1.7",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "6",
                      "device_logical_id": "6"
                    },
                   {
                      "device_id": "7",
                      "device_ip": "1.1.1.8",
                      "super_device_id": "xxxxxxxxxx",
                      "rank_id": "7",
                      "device_logical_id": "7"
                    }
                 ]
               }
             ],
             "super_pod_list": [
               {
                 "super_pod_id": "0",
                 "server_list": [
                   {
                     "server_id": "127.0.0.1"
                    }
                  ]
               }
             ]
           },
           {
             "group_id": "1",
             "server_count": "1",
             "server_list": [
               {
                 "server_ip": "127.0.0.1"
               }
             ]
           },
           {
             "group_id": "0",
             "server_count": "1",
             "server_list": [
               {
                 "server_ip": "127.0.0.1"
               },
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
     | server_count      | string       | 各组件的进程总数。                                           |
     | server_list       | json对象数组 | 各组件的进程部署信息。Controller实例：列表有效长度为[0, 1]。Coordinator实例：列表有效长度为[0, 1]。Server实例：列表有效长度为[0, npu_num]（npu_num为npu个数）。 |
     | server_id         | string       | 组件节点的主机IP。                                           |
     | server_ip         | string       | 组件节点的IP地址。                                           |
     | predict_port      | string       | EndPoint提供的业务面RESTful接口绑定的端口号。                |
     | mgmt_port         | string       | EndPoint提供的内部接口绑定的端口号。                         |
     | metric_port       | string       | 服务管控指标接口（普罗格式）端口号。                         |
     | inter_comm_port   | string       | 集群内部实例间的通信端口。                                   |
     | device            | json对象数组 | NPU设备信息，仅Server有此属性。列表有效长度[1, 128]。        |
     | device_id         | string       | NPU的设备ID。                                                |
     | device_ip         | string       | NPU的IP地址。                                                |
     | super_device_id   | string       | 超节点场景下NPU设备ID；该参数只涉及Atlas 800I A3 超节点服务器。 |
     | rank_id           | string       | NPU的逻辑ID，即Server所在Pod内可见的卡设备的序列ID。         |
     | device_logical_id | string       | NPU的逻辑ID，即Server所在Pod内可见的卡设备的序列ID。         |
     | super_pod_list    | string       | 超节点列表；该参数只涉及Atlas 800I A3 超节点服务器。         |
     | super_pod_id      | string       | 当前超节点ID；该参数只涉及Atlas 800I A3 超节点服务器。       |

   - 如需要进入容器查找更多定位信息，则执行以下命令：

     ```
     kubectl exec -it mindie-server-7b795f8df9-vl9hv -n mindie -- bash
     ```

10. 通过脚本示例提供的generate_stream.sh发起流式推理请求。

    部署成功后，会在节点上开放31015端口用于推理业务接口，需要修改generate_stream.sh中的IP地址为集群管理节点宿主机的IP地址，如Coordinator组件启用了HTTPS，需要配置相关的证书；如使用HTTP，需修改脚本中的HTTPS为HTTP，并删除证书相关配置。

    ```
    bash generate_stream.sh
    ```

    <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
    <strong>💡 说明</strong>
    <ul style="margin:8px 0;padding-left:20px;">HTTP协议存在安全风险，建议您使用HTTPS安全协议。 </ul></div>

11. 卸载PD集群。

    如需停止PD服务或者修改业务配置重新部署实例，需要调用以下命令卸载已部署的实例，重新部署请执行8。

    ```
    bash delete.sh mindie ./
    ```

    <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
    <strong>💡 说明</strong>
    <ul style="margin:8px 0;padding-left:20px;">
        <li>delet.sh卸载脚本需要在examples/kubernetes_deploy_scripts目录下执行，否则无法停止服务并报错。</li>
    	<li>mindie为1创建的命名空间，请根据实际的命名空间进行替换。</li>
        </ul></div>
