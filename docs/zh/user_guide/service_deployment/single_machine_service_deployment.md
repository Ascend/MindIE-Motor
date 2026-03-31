# 场景介绍

单机服务部署为非分布式实例部署的场景，即在一个计算节点内可部署一个完整独立的Server推理服务实例。根据设备资源情况，同一个计算节点可部署多个Server服务实例，也支持在多个计算节点上部署多个服务实例。

**Coordinator作为对外服务入口**

推理请求：通过第三方平台的调度入口（由用户部署平台而定，比如K8s的调度入口或MA的调度入口等），将所有请求发送给Coordinator，Coordinator基于本身支持的负载调度算法，调度请求发送给各个Server实例。具体部署详情请参见[使用kubectl部署服务示例](#使用kubectl部署服务示例)章节。使用该部署方式部署单机（非分布式）服务时，其支持的接口请参见[RESTful接口API](https://www.hiascend.com/document/detail/zh/mindie/230/mindiemotor/motordev/mindie_service0256.html)。

**图 2**  Coordinator作为对外服务入口

![](../../figures/coordinator_external_service.png)

单机部署场景支持的调度算法如下表所示：

|调度算法|含义|部署建议|
|--|--|--|
|cache_affinity|Cache亲和调度算法：当前只支持OpenAI多轮会话场景的亲和调度算法。|OpenAI多轮会话场景，推荐配置。|
|round_robin|轮询调度算法：非OpenAI多轮会话场景的调度算法。|使用非OpenAI多轮会话接口时默认执行此算法，用户无须配置。|

>[!NOTE]说明
>为保障业务稳定运行，用户应严格控制自建Pod的权限，避免高权限Pod修改MindIE内部参数而导致异常。

# 使用kubectl部署服务示例

>[!NOTE]说明
>
>- 该示例支持部署单机版的Server、Coordinator和Controller。
>- Atlas 800I A2 推理服务器需要配置卡IP并安装Ascend Operator组件；Atlas 300I Duo 推理卡+Atlas 800 推理服务器（型号 3000）无需配置和安装。
>- 当前部署脚本不支持NPU故障重调度场景。

本小节使用MindIE Motor的run包安装目录（./mindie-service/latest/examples/kubernetes_deploy_scripts）中的脚本实现调用kubectl进行服务部署，脚本提供一键式部署和卸载单机形态集群功能，集群管理员用户可参考这些脚本线下使用K8s kubectl工具部署服务。

集群管理员用户只需在管理节点完成启动脚本编写、业务配置和K8s配置，再调用部署脚本，实现自动下发业务配置和启动脚本，自动生成包含节点设备信息的ranktable，并调度Pod到计算节点。

脚本文件所在目录结构如下所示：

```txt
├── boot_helper
│   ├── boot.sh
│   ├── gen_config_single_container.py
│   ├── get_group_id.py
│   └── update_mindie_server_config.py
├── chat.sh
├── conf
├── delete.sh
├── deployment
│   ├── mindie_ms_controller.yaml
│   ├── mindie_ms_coordinator.yaml
│   ├── mindie_server_heterogeneous.yaml
│   ├── mindie_server.yaml
│   └── mindie_service_single_container.yaml
├── deploy.sh
├── generate_stream.sh
├── gen_ranktable_helper
│   ├── gen_global_ranktable.py
│   └── global_ranktable.json
└── log.sh
```

**关键目录及文件解释如下所示：**

- `boot_helper`：包含容器启动脚本`boot.sh`，获取group_id，刷新环境变量到配置文件，设置启动程序的环境变量等，用户可根据需要在这里调整日志等级等。
- `chat.sh`：使用curl发送HTTP请求给推理服务的简单对话示例。
- `conf`：集群管理组件和Server的主要业务配置文件，PD分离管理调度策略和模型相关配置。
- `delete.sh`：卸载脚本，一键卸载所有MindIE组件。
- `deployment`:  K8s部署任务定义，配置NPU资源使用量，实例数，镜像等。
- `deploy.sh`：部署入口脚本，一键拉起所有MindIE组件。
- `generate_stream.sh`：使用curl发送HTTPS请求给推理服务的流式响应示例。
- `gen_ranktable_helper`：生成global ranktable的工具，用户无需感知。
- `log.sh`：查询Pod的打印日志，可查询到部署的所有Pod的日志。

**前提条件**

已参照[Kubernetes安装与配置](./environment_preparation.md#kubernetes安装与配置)、[MindCluster组件安装](./environment_preparation.md#mindcluster组件安装)和[准备MindIE镜像](./preparing_mindie_image.md)完成K8s的安装配置、MindCluster组件的安装和MindIE镜像制作。

<br>

**操作步骤**

部署样例如下所示，以下操作均在部署脚本路径下完成：

1. <a id="li121732348534"></a>创建mindie命名空间，默认值为mindie，如需创建其它名称请自行替换。

    ```bash
    kubectl create namespace mindie
    ```

2. 配置集群管理组件的Controller和Coordinator子组件，将这两个组件的部署模式配置为单机（非分布式）服务部署模式，其必配参数如下所示。
   - 配置ms_controller.json文件，参数详情请参见[配置说明](../cluster_management_component/controller.md#配置说明)章节。

        将部署模式配置为单机（非分布式）服务部署模式，需配置以下参数。

        ```bash
        "deploy_mode"= "single_node"
        ```

   - 配置ms_coordinator.json文件，参数详情请参见[配置说明](../cluster_management_component/coordinator.md#配置说明)章节。
        - 配置单机（非分布式）服务部署模式，需配置以下参数。

            ```bash
            "deploy_mode"= "single_node"
            ```

        - 配置OpenAI多轮会话Cache亲和调度场景，需配置以下参数。

            ```bash
            "scheduler_type": "default_scheduler",
            "algorithm_type": "cache_affinity",
            ```

3. 配置Server服务启动的config.json配置文件，单机（非分布式）服务部署模式需要配置以下参数，具体参数解释请参见《MindIE LLM开发指南》中的“核心概念与配置 > 配置参数说明（服务化）”章节。
    - modelWeightPath：模型权重文件目录配置，默认情况下脚本会挂载物理机的/data目录，该参数需配置为/data路径下的模型权重路径，确保集群可调度计算节点在该路径下存在模型文件。
    - worldSize：配置一个实例需要占用的卡数；例如配置为"2"，表示使用两张卡。
    - npuDeviceIds：卡号配置成从0开始编号，总数与worldSize一致，例如配置为\[[0,1]]。
    - inferMode：设置为"standard"。
    - 使能Prefix Cache：
        - 在ModelDeployConfig中的ModelConfig下添加以下配置：

            ```bash
            "plugin_params": "{\"plugin_type\":\"prefix_cache\"}"
            ```

        - 在ScheduleConfig中添加以下信息：

            ```bash
            "enablePrefixCache": true
            ```

4. 配置http_client_ctl.json配置文件，该配置文件为集群启动、存活、就绪探针HTTP客户端工具的配置文件。

    "tls_enable"为控制是否使用HTTPS的开关：

    - true：集群内MindIE组件使用HTTPS接口；需要导入证书到容器内，配置相应的证书路径。
    - false：集群内MindIE组件使用HTTP接口；无需准备证书文件。

    >[!NOTE]说明
    >建议用户打开tls_enable，确保通信安全，如果关闭则存在较高的网络安全风险。

5. 配置kubernetes Deployment。

    在部署脚本目录中的deployment目录下找到mindie_server.yaml、mindie_ms_coordinator.yaml和mindie_ms_controller.yaml文件。

    >[!NOTE]说明
    >- 本脚本仅作为一个部署参考，Pod容器的安全性由用户自行保证，实际生产环境请针对镜像和Pod安全进行加固。
    >- 用户在使用kubectl部署Deployment时，需要修改deployment的配置yaml文件，请避免使用危险配置，确保使用安全镜像（非root权限用户），配置安全的Pod上下文。
    >- 用户应挂载安全路径（非软链接，非系统危险路径，非业务敏感路径），并设置合理的文件目录权限，避免挂载/home等公共目录，防止被非法用户篡改，导致容器逃逸问题。

    - mindie_server.yaml文件主要配置的字段如下所示：
        - replicas：配置总实例数。
        - `huawei.com/Ascend910`：resources资源请求，配置一个实例占用的910 NPU卡数，与MindIE Serve的config.json配置文件中worldSize参数配置的卡数保持一致。
        - image：配置镜像名。
        - nodeSelector：节点选择，用户期望调度的节点，通过节点标签实现。
        - ring-controller.atlas：根据实际使用的设备型号配置为ascend-9xxx或ascend-3xxx。
        - startupProbe：启动探针，默认启动时间为500秒，如果在该时间内服务未启动成功，Pod将会自动重启。请用户根据实际场景设置合理的启动时间。

    - mindie_ms_coordinator.yaml和mindie_ms_controller.yaml文件主要配置的字段如下所示：

        - image：配置镜像名。
        - nodeSelector：节点选择，用户期望调度的节点，通过节点标签实现。

        >[!NOTE]说明
        >- mindie_ms_coordinator.yaml文件需特别关注livenessProbe（存活探针）参数，在高并发推理请求场景下，可能会因为探针超时，从而被K8s识别为Pod不存活，导致K8s重启Coordinator容器，建议用户谨慎开启livenessProbe（存活探针）。
        >- 为了支持MindIE Controller故障恢复功能，mindie_ms_controller.yaml文件中volumes参数的name: status-data所配置的目录，必须为待部署计算节点（该计算节点通过nodeSelector参数指定）上真实存在，且volumeMounts参数中的name: status-data挂载路径必须为：/_MindIE Motor安装路径_/logs。

6. 拉起集群。<a id="li1651115619332"></a>

    首先配置容器内mindie安装的目录：根据制作镜像时实际的安装路径，修改MINDIE_USER_HOME_PATH的value值，如安装路径是/xxx/Ascend/mindie，则配置为/xxx 。

    ```bash
    export MINDIE_USER_HOME_PATH={镜像的安装路径}
    ```

    使用以下命令拉起集群。

    ```bash
    bash deploy.sh
    ```

    执行后，会同步等待global ranktable生成完成，如长时间处于阻塞状态，请ctrl+c中断后查看集群Pod状态，进行下一步的调试定位。

    >[!NOTE]说明
    >- 集群默认刷新挂载到容器内的configmap的频率是60s，如遇到容器内打印“status of ranktable is not completed”日志信息的时间偏久，可在每个待调度的计算节点修改kubelet同步configmap的周期，即修改/var/lib/kubelet/config.yaml中的syncFrequency参数，将周期减少到5s，注意此修改可能影响集群性能。
    >
    >    ```bash
    >    syncFrequency: 5s
    >    ```
    >
    >    然后使用以下命令重启kubelet：
    >
    >    ```bash
    >    swapoff -a
    >    systemctl restart  kubelet.service
    >    systemctl status kubelet
    >    ```
    >
    >- 确保Docker配置了标准输出流写入到文件的最大规格，防止磁盘占满导致Pod被驱逐。
    >    需在待部署服务的计算节点上修改Docker配置文件后重启Docker：
    >    1. 使用以下命令打开daemon.json文件。
    >
    >        ```bash
    >        vim /etc/docker/daemon.json
    >        ```
    >
    >        在daemon.json文件中添加日志选项"log-opts"，具体内容如下所示。
    >
    >        ```bash
    >        "log-opts":{"max-size":"500m", "max-file":"3"}
    >        ```
    >
    >        参数解释：
    >        max-size=500m：表示一个容器日志大小上限是500M。
    >        max-file=3：表示一个容器最多有三个日志，超过会自动滚动更新。
    >    2. 使用以下命令重启Docker.
    >
    >        ```bash
    >        systemctl daemon-reload
    >        systemctl restart docker
    >        ```

7. 查看集群状态。

    通过kubectl命令查看Pod状态：

    ```bash
    kubectl get pods -n mindie
    ```

    如启动4个Server实例，回显如下所示：

    ```bash
    NAME                                     READY   STATUS    RESTARTS   AGE    IP               NODE       NOMINATED NODE   READINESS GATES
    mindie-ms-controller-7845dcd697-h4gw7    1/1     Running   0          145m   xxx.xxx.xxx    ubuntu10   <none>           <none>
    mindie-ms-coordinator-6bff995ff8-l6fwz   1/1     Running   0          145m   xxx.xxx.xxx    ubuntu10   <none>           <none>
    mindie-llm-7b795f8df9-2xvh4           1/1     Running   0          145m   xxx.xxx.xxx   ubuntu     <none>           <none>
    mindie-llm-7b795f8df9-j4z7d           1/1     Running   0          145m   xxx.xxx.xxx   ubuntu     <none>           <none>
    mindie-llm-7b795f8df9-v2tcz           1/1     Running   0          145m   xxx.xxx.xxx   ubuntu     <none>           <none>
    mindie-llm-7b795f8df9-vl9hv           1/1     Running   0          145m   xxx.xxx.xxx   ubuntu     <none>           <none>
    ```

    - 以mindie-ms-controller开头的为集群管理组件的Controller控制器组件。
    - 以mindie-ms-coordinator开头的为集群管理组件的Coordinator调度器组件。
    - 以mindie-llm开头的为Server推理服务。

    如观察Pod进入Running状态，表示Pod容器已成功被调度到节点并正常启动，但还需要进一步确认业务程序是否启动成功。

    通过脚本示例提供的log.sh脚本可查询这些Pod的标准输出日志，查看是否出现异常：

    ```bash
    bash log.sh
    ```

    - 如需要查询具体某个Pod（如上面mindie-server-7b795f8df9-2xvh4）的日志，则执行以下命令：

        ```bash
        kubectl logs mindie-server-7b795f8df9-2xvh4 -n mindie
        ```

    - 如需要进入容器查找更多定位信息，则执行以下命令：

        ```bash
        kubectl exec -it mindie-server-7b795f8df9-2xvh4 -n mindie -- bash
        ```

8. 通过脚本示例提供的chat.sh发起推理请求。

    需修改chat.sh中的IP地址为集群管理节点宿主机的IP地址，其中role配置为user。

    ```bash
    bash chat.sh
    ```

9. 卸载集群。

    如需停止单机服务或者修改业务配置重新部署实例，需要调用以下命令卸载已部署的实例，重新部署请执行[6](#li1651115619332)。

    ```bash
    bash delete.sh
    ```

    >[!NOTE]说明
    >mindie为[1](#li121732348534)创建的命名空间，请根据实际的命名空间进行替换。
