# 功能介绍

基于Kubernetes（简称K8s）平台，整合Kubernetes生态组件（包括昇腾MindCluster组件），提供对Server推理服务的一键式自动部署能力，用户可以通过deployer提供的命令行工具，实现一键自动化部署和运维管理，简化用户部署Server推理服务集群的流程，以下是集群管理组件部署面支持的特性框架图。

**图 1**  特性框架图

![](../../figures/feature_framework_diagram.png)

支持的特性如下所示：

- 支持命令行操作界面；
- 提供单模型单机的部署能力；

- 支持单模型分布式部署能力；
- 支持滚动更新（仅单机支持）、NPU故障重调度（仅单机支持）、Server进程故障重启等能力。

# 配置说明

Deployer的服务端组件发布件为ms_server，是一个HTTP服务端，相关命令解释如[表1](#table9153583215)所示。

**表 1**  <a id="table9153583215"></a>集群管理组件服务端相关命令

|指令|说明|
|--|--|
|./ms_server ms_server.json|启动集群管理组件服务端命令。ms_server是MindIE Motor中的集群管理组件服务端组件，命令中ms_server.json配置文件的具体配置和参数说明请参见[ms_server.json配置文件](#ms_server.json)。<br>ms_server.json需要不大于640权限，否则启动失败。|
|./ms_server -h/--help|查询命令使用方法。|
|进程退出信号|./ms_server进程注册了信号处理函数捕获SIGINT和SIGTERM信号，用户启动ms_server后，通过发送SIGINT，SIGTERM信号可正常停止该进程。|

<br>

**ms_server.json配置文件**<a id="ms_server.json"></a>

```
{
    "ip": "127.0.0.1",
    "port": 9789,
    "k8s_apiserver_ip": "127.0.0.1",
    "k8s_apiserver_port": 6443,
    "log_info": {
        "log_level": "INFO",
        "run_log_path": "/var/log/mindie-ms/run/log.txt",
        "operation_log_path": "/var/log/mindie-ms/operation/log.txt"
    },
    "ms_status_file": "/var/lib/mindie-ms/status.json",
    "server_tls_items": {
        "ca_cert" : "./security/msserver/security/certs/ca.pem",
        "tls_cert": "./security/msserver/security/certs/cert.pem",
        "tls_key": "./security/msserver/security/keys/cert.key.pem",
        "tls_passwd": "./security/msserver/security/pass/key_pwd.txt",
        "kmcKsfMaster": "./security/msserver/tools/pmt/master/ksfa",
        "kmcKsfStandby": "./security/msserver/tools/pmt/standby/ksfb",
        "tls_crl": ""
    },
    "client_k8s_tls_enable": true,
    "client_k8s_tls_items": {
        "ca_cert" : "./security/kubeclient/security/certs/ca.pem",
        "tls_cert": "./security/kubeclient/security/certs/cert.pem",
        "tls_key": "./security/kubeclient/security/keys/cert.key.pem",
        "tls_passwd": "./security/kubeclient/security/pass/key_pwd.txt",
        "kmcKsfMaster": "./security/kubeclient/tools/pmt/master/ksfa",
        "kmcKsfStandby": "./security/kubeclient/tools/pmt/standby/ksfb",
        "tls_crl": ""
    },
    "client_mindie_server_tls_enable": true,
    "client_mindie_tls_items": {
        "ca_cert" : "./security/mindieclient/security/certs/ca.pem",
        "tls_cert": "./security/mindieclient/security/certs/cert.pem",
        "tls_key": "./security/mindieclient/security/keys/cert.key.pem",
        "tls_passwd": "./security/mindieclient/security/pass/key_pwd.txt",
        "kmcKsfMaster": "./security/mindieclient/tools/pmt/master/ksfa",
        "kmcKsfStandby": "./security/mindieclient/tools/pmt/standby/ksfb",
        "tls_crl": ""
    }
 }
```

**表 2**  ms_server.json参数解释

|参数|类型|说明|
|--|--|--|
|ip|String|必填。<br>集群管理组件服务端IP；支持IPv4或IPv6，如配置了环境变量MINDIE_MS_SERVER_IP，则优先从环境变量读取。<br>考虑到集群管理组件运行业务的安全问题，建议不对外提供服务，配置成127.0.0.1；使用其他IP对外暴露接口将有安全风险，请用户需要慎重使用。|
|port|Int|必填。<br>集群管理组件服务端对外端口，取值范围：[1024，65535]，默认值9789。|
|k8s_apiserver_ip|String|必填。<br>Kubernetes管理节点的物理机IP地址。|
|k8s_apiserver_port|Int|必填。<br>Kubernetes对外访问的HTTPS端口，取值范围：[1024，65535]，默认值：6443（该端口为K8s的HTTPS端口）。|
|log_info|Object|集群管理组件服务端日志配置信息，包括log_level、run_log_path和operation_log_path子参数，详情请参见[表3 log_info子参数解释](#table5645645)。<br>建议使用环境变量配置日志，详情请参见日志配置。|
|ms_status_file|String|必填。<br>用于服务状态保存的文件路径。<br>用户需要提前创建该文件所在的目录，第一次启动ms_server时会自动创建该文件，用户不需要自己创建，也不能手动修改。<br>后续部署的服务状态信息将由ms_server自动写入该文件，ms_server重启将读取该文件恢复状态。该文件的格式如下[表4 status持久化业务状态文件解释](#table5649865)|
|server_tls_items|Object|集群管理组件服务端tls配置，详情请参见[表5 server_tls_items子参数解释](#table5643615)。|
|client_k8s_tls_enable|Bool|必填。<br>集群管理组件与Kubernetes对接的客户端是否需要开启tls安全通信，建议用户打开，确保与kubernetes通信安全。<br>-client_k8s_tls_enable为true，访问Kubernetes的HTTPS接口。<br>-client_k8s_tls_enable为false，访问Kubernetes的HTTP接口。|
|client_k8s_tls_items|Object|Kubernetes客户端tls配置，当client_k8s_tls_enable为true时，该参数中的key值为必填项，详情请参见[表6 client_k8s_tls_items子参数解释](#table5698623)。|
|client_mindie_server_tls_enable|Bool|必填。<br>集群管理组件对接Server接口是否开启tls安全通信。建议用户打开，确保集群管理组件管理节点与计算节点Server间通信安全。|
|client_mindie_tls_items|Object|Server客户端tls配置，当client_mindie_server_tls_enable为true时，该参数中的key值为必填项，详情请参见[表7 client_mindie_tls_items子参数解释](#table5689752)。|


**表 3**  log_info子参数解释<a id="table5645645"></a>

|参数|类型|说明|
|--|--|--|
|log_level|String|选填。支持等级如下：<br>-DEBUG<br>-INFO<br>-WARN<br>-ERROR<br>-CRITICAL<br>如设置环境变量MINDIE_LOG_LEVEL或MINDIEMS_LOG_LEVEL，则优先读取环境变量的值。默认日志等级为INFO级别。|
|run_log_path|String|集群管理组件服务端运行日志保存文件路径，选填。<br>可访问文件。默认路径请参考[日志配置](./log_configuration.md)，如设置环境变量MINDIE_LOG_PATH，则优先读取环境变量的值。<br>Kubernetes容器化部署集群管理组件服务端时，必须为/var/log/mindie-ms/run/log.txt。|
|operation_log_path|String|集群管理组件服务端操作保存文件路径，选填。<br>可访问文件。默认路径请参考[日志配置](./log_configuration.md)，如设置环境变量MINDIE_LOG_PATH，则优先读取环境变量的值.<br>Kubernetes容器化部署集群管理组件服务端时，必须为/var/log/mindie-ms/operation/log.txt。|


**表 4**  status持久化业务状态文件解释<a id="table5649865"></a>

|参数|类型|说明|
|--|--|--|
|server_list|Array|表示集群管理组件管理的推理服务列表清单。未部署服务时为空。|
|namespace|String|表示服务的命名空间。|
|replicas|Int|推理实例数。|
|server_name|Sting|推理服务名。|
|server_type|String|服务类型，当前只支持多机推理服务，取值mindie_cross_node。|
|use_service|Bool|是否使用kubernetes的Service暴露服务端口。|


**表 5**  server_tls_items子参数解释<a id="table5643615"></a>

|参数|类型|说明|
|--|--|--|
|ca_cert|String|必填。<br>集群管理组件HTTPS服务端的CA证书，使用kubernetes集群CA证书。集群管理组件服务端会校验客户端证书信息的CN为msclientuser，O为msgroup，请确保客户端的证书是kubernetes CA所信任的，且包含这些信息。<br>集群管理组件服务端ca根证书文件路径，该路径真实存在且可读。|
|tls_cert|String|必填。<br>使用kubernetes集群CA证书签发的服务证书。<br>集群管理组件服务端tls证书文件路径，该路径真实存在且可读。|
|tls_key|String|必填。<br>使用kubernetes集群CA证书签发的服务证书私钥。<br>集群管理组件服务端tls私钥文件路径，该路径真实存在且可读。|
|tls_passwd|String|必填。<br>集群管理组件HTTPS服务端的KMC加密的私钥口令的文件路径。|
|kmcKsfMaster|String|必填。<br>集群管理组件HTTPS服务端加密口令的KMC密钥库文件。|
|kmcKsfStandby|String|必填。<br>集群管理组件HTTPS服务端加密口令的KMC standby密钥库备份文件。|
|tls_crl|String|必填。<br>集群管理组件服务端校验客户端的证书吊销列表CRL文件路径，要求该文件真实存在且可读。若为空，则不进行吊销校验。|


**表 6**  client_k8s_tls_items子参数解释<a id="table5698623"></a>

|参数|类型|说明|
|--|--|--|
|ca_cert|String|必填。<br>使用kubernetes集群CA证书。<br>集群管理组件和Kubernetes对接的客户端的ca根证书文件路径，该路径真实存在且可读。|
|tls_cert|String|必填。<br>使用kubernetes集群CA证书签发的客户端证书，证书的CN与通过kubernetes RBAC机制创建的用户名一致。<br>集群管理组件和Kubernetes对接的客户端的tls证书文件路径，该路径真实存在且可读。|
|tls_key|String|必填。<br>使用kubernetes集群CA证书签发的客户端证书私钥。<br>集群管理组件和Kubernetes对接的客户端的tls私钥文件路径，该路径真实存在且可读。|
|tls_passwd|String|必填。<br>kmc加密的私钥口令的文件路径。|
|kmcKsfMaster|String|必填。<br>Server作为kube api-server的客户端加密口令的KMC密钥库文件。|
|kmcKsfStandby|String|必填。<br>Server作为kube api-server的客户端加密口令的KMC密钥库备份文件。|
|tls_crl|String|必填。<br>集群管理组件服务端校验Kubernetes API Server的证书吊销列表CRL文件路径，要求该文件真实存在且可读。若为空，则不进行吊销校验。|


**表 7**  client_mindie_tls_items子参数解释<a id="table5689752"></a>

|参数|类型|说明|
|--|--|--|
|ca_cert|String|必填。<br>集群管理组件和Server对接的客户端的ca根证书文件路径，该路径真实存在且可读。|
|tls_cert|String|必填。<br>集群管理组件和Server对接的客户端的tls证书文件路径，该路径真实存在且可读。|
|tls_key|String|必填。<br>集群管理组件和Server对接的客户端的tls私钥文件路径，该路径真实存在且可读。|
|tls_passwd|String|必填。<br>kmc加密的私钥口令的文件路径。|
|kmcKsfMaster|String|必填。<br>Server作为kube api-server的客户端加密口令的KMC密钥库文件。|
|kmcKsfStandby|String|必填。<br>Server作为kube api-server的客户端加密口令的KMC密钥库备份文件。|
|tls_crl|String|必填。<br>集群管理组件服务端校验Server的证书吊销列表CRL文件路径，要求该文件真实存在且可读。若为空，则不进行吊销校验。|

<br>

# RESTful接口API

## 推理服务部署接口

**接口功能**

推理服务部署，发起部署请求，异步接口，部署配置信息请参见[表2 infer_server.json配置文件参数解释](../service_deployment/single_machine_service_deployment.md#table67213616914)。

<br>

**接口格式**

操作类型：POST

```
URL：https://{ip}:{port}/v1/servers
```

>[!NOTE]说明
>-   滚动更新配置max\_unavailable和max\_surge不能全为0，另外若配置max\_unavailable \> 0, max\_surge = 0，但是replicas \* max\_unavailable向下取整为0，那么此时将触发强制更新。
>-   当前滚动更新过程使用kubernetes的原生Service进行负载均衡，若用户使用http长链接发送请求，在更新过程中Service不会主动中断请求客户端与正在退出的旧推理服务实例的长链接，新的请求进入旧实例将被拒绝，导致请求失败。建议用户在遇到中断后主动重新建链或使用短链接（一次请求响应后断链）发送请求。

<br>

**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/servers
```

响应样例：

```
{
    "message": "creating the server!",
    "status": "0"
}
```

<br>

**输出说明**

**表 1**  请求响应状态码

|code|说明|
|--|--|
|200|ok：请求成功。|
|400|bad_request：请求失败，非法请求。|
|404|not_found：请求失败，找不到资源。|
|500|internal_server_error：请求失败，内部出现错误。|

<br>

## 推理服务查询接口

**接口功能**

查询服务的部署状态，包括部署阶段、就绪状态、模型信息等。

<br>

**接口格式**

操作类型：GET

```
URL：https://{ip}:{port}/v1/servers/{server_name}
```

<br>

**请求参数**

无

<br>

**使用样例**

请求样例：

```
GET https://{ip}:{port}/v1/servers/{server_name}
```

响应样例：

```
{
    "data": {
        "instances_status": [
            {
                "liveness": true,
                "pod_name": "mindie-server-zsm-586c8fb5f8-vtx2n",
                "readiness": true
            }
        ],
        "model_info": {
            "docker_label": null,
            "max_batch_total_tokens": 8192,
            "max_best_of": 1,
            "max_concurrent_requests": 200,
            "max_input_length": 2048,
            "max_stop_sequences": null,
            "max_waiting_tokens": null,
            "models": [
                {
                    "max_total_tokens": 2560,
                    "model_device_type": "npu",
                    "model_dtype": "float16",
                    "model_id": "llama3_70b",
                    "model_pipeline_tag": "text-generation",
                    "model_sha": null
                }
            ],
            "sha": null,
            "validation_workers": null,
            "version": "1.0.RC3",
            "waiting_served_ratio": null
        },
        "server_name": "mindie-server"
    },
    "message": "success",
    "status": "0"
}
```

重要参数解释：

liveness：表示服务存活状态，取值如下：

- true：表示服务存活。
- false：表示服务未存活。

readiness：表示服务实例启动状态，取值如下：

- true：表示服务实例已启动完成并进入就绪状态。
- false：表示服务实例未启动完成。

<br>

**输出说明**

**表 1**  请求响应状态码

|code|说明|
|--|--|
|200|ok：请求成功。|
|400|bad_request：请求失败，非法请求。|
|404|not_found：请求失败，找不到资源。|
|500|internal_server_error：请求失败，内部出现错误。|


## 推理服务卸载接口

**接口功能**

卸载服务，删除相关已创建资源。

<br>

**接口格式**

操作类型：DELETE

```
URL：https://{ip}:{port}/v1/servers/{server_name}
```

<br>

**请求参数**

无

<br>

**使用样例**

请求样例：

```
DELETE https://{ip}:{port}/v1/servers/{server_name}
```

响应样例：

```
{
    "message": "succeed to clear resources",
    "status": "0"
}
```
<br>

**输出说明**

**表 1**  请求响应状态码

|code|说明|
|--|--|
|200|ok：请求成功。|
|400|bad_request：请求失败，非法请求。|
|404|not_found：请求失败，找不到资源。|
|500|internal_server_error：请求失败，内部出现错误。|

<br>

## 推理服务更新接口

**接口功能**

用于强制滚动更新服务，用户更新镜像后可通过调用该接口更新服务，更新的策略在[推理服务部署接口](#推理服务部署接口)服务部署时指定。

>[!NOTE]说明
>当前只支持单机（非分布式）部署场景的更新。

<br>

**接口格式**

操作类型：POST

```
URL：https://{ip}:{port}/v1/servers/{server_name}
```

<br>

**请求参数**

|参数|类型|说明|
|--|--|--|
|server_name|string|必填。<br>待更新的服务名称，需与服务部署配置指定的server_name一致。|

<br>

**使用样例**

准备服务更新update_server.json配置文件，如下所示：

```
{
    "server_name": "mindie-server"
}
```

请求样例：

```
POST https://{ip}:{port}/v1/servers/{server_name}
```

响应样例：

```
{
    "message": "update server success.",
    "status": "0"
}
```

<br>

**输出说明**

**表 1**  请求响应状态码

|code|说明|
|--|--|
|200|ok：请求成功。|
|400|bad_request：请求失败，非法请求。|
|404|not_found：请求失败，找不到资源。|
|500|internal_server_error：请求失败，内部出现错误。|





# 错误码说明

当前只会返回两种错误码，如下所示。

具体错误客户端会在屏幕打印异常信息；服务端的错误信息请参见[表3 log_info子参数解释](#table5645645)中"run_log_path"参数配置的文件路径。

<li>0：表示服务正常。
<li>1：表示异常情况。




