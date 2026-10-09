# MindIE Motor通信矩阵

本表依据MindIE Motor 3.2.0版本实现编写，以Kubernetes部署为例记录Motor组件发起的网络连接和对外监听端口。表中的“源设备”是建立连接的一方；消息或事件的数据流向可能与建立连接方向相反。源端口由操作系统分配，不承诺固定的临时端口范围。

Kubernetes中，组件内部访问使用Pod IP或Service地址；对外访问使用节点IP和NodePort。默认映射为推理`31015 → 1025`、Coordinator观测`31017 → 1027`、Controller观测`31027 → 1027`。NodePort可由部署配置修改，冲突时部署器也可能重映射；实际端口以生成的Service YAML和运行配置为准。Docker、独立部署等场景没有这些Kubernetes NodePort，应使用对应容器或进程的实际监听地址。

>[!NOTE]说明
>datadist、HCCL、LCCL和ATB相关通信矩阵请参考[CANN 通信矩阵](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/latest/maintenref/refdoc/refer001.html)。原生vLLM/SGLang的KV传输、分布式通信及PD bootstrap等端口还取决于引擎配置，不能统一套用Motor业务端口规则；配置字段见[全量配置说明](configuration/config_reference.md)。

## 常规通信

|源设备|源IP地址|源端口|目的设备|目的IP地址|目的端口（侦听）|协议|端口说明|侦听端口是否可更改|认证方式|加密方式|所属平面|版本|特殊场景|备注|
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
|NodeManager|所在Pod IP|系统分配|Controller|Controller服务地址|默认1026|TCP|HTTP：实例注册、重注册、心跳及故障上报|是|默认无；可选TLS服务端证书校验|默认无；可选TLS|管理面|MindIE Motor 3.2.0|由Controller管理实例的部署|Controller端口配置：`api_config.controller_api_port`|
|Coordinator|所在Pod IP|系统分配|Controller|Controller服务地址|默认1026|TCP|HTTP：告警等信息上报|是|默认无；可选TLS服务端证书校验|默认无；可选TLS|管理面|MindIE Motor 3.2.0|由Controller管理实例的部署|Controller端口配置：`api_config.controller_api_port`|
|Controller|所在Pod IP|系统分配|Coordinator管理API|Coordinator服务地址|默认1026|TCP|HTTP：实例变更推送、状态探活及管理请求|是|默认无；部分敏感接口可选管理API Key；可选TLS服务端证书校验|默认无；可选TLS|管理面|MindIE Motor 3.2.0|由Controller管理实例的部署|Coordinator管理端口配置：`api_config.coordinator_api_mgmt_port`|
|Controller|所在Pod IP|系统分配|Coordinator观测API|Coordinator服务地址|默认1027|TCP|HTTP：读取`/metrics`指标|是|`/metrics`不校验API Key；可选TLS服务端证书校验|默认无；可选TLS|观测面|MindIE Motor 3.2.0|由Controller管理实例的部署|Coordinator观测端口配置：`api_config.coordinator_obs_port`|
|Controller|所在Pod IP|系统分配|NodeManager管理API|NodeManager Pod IP|默认1026|TCP|HTTP：引擎启动、停止、状态查询及故障恢复指令|是|默认无；可选TLS服务端证书校验|默认无；可选TLS|管理面|MindIE Motor 3.2.0|由Controller管理实例的部署|NodeManager端口配置：`api_config.node_manager_port`|
|Coordinator、Controller|所在Pod IP|系统分配|ETCD|ETCD服务地址|默认2379|TCP|客户端连接：主备选举、租约及持久化状态|是|取决于ETCD及客户端证书配置|默认无；可选TLS|管理面|MindIE Motor 3.2.0|Controller/Coordinator启用主备或状态持久化时|ETCD端口配置：`etcd_config.etcd_port`；TLS配置：`etcd_tls_config`|
|Coordinator|所在Pod IP|系统分配|原生vLLM/SGLang引擎|引擎Pod IP|实例上报的业务端口|TCP|HTTP/HTTPS：P、D或U实例推理、模型查询及`/metrics`采集|是|由原生引擎配置决定|默认无；按引擎配置可选TLS|数据面|MindIE Motor 3.2.0|vLLM/SGLang推理服务（PD分离或混部）|同Pod内端点序号从0开始，初始端口为`base_port + 2 × 序号`，默认`base_port=10000`；启用端口自动分配时可能调整，以实例注册信息为准|
|NodeManager|所在Pod IP|系统分配|本Pod原生引擎|引擎Pod IP|实例业务端口|TCP|HTTP/HTTPS：健康检查、虚拟推理、容量压力指标查询及原生故障恢复接口，与推理共用业务端口|是|由原生引擎配置决定|默认无；按引擎配置可选TLS|内部接口|MindIE Motor 3.2.0|引擎由NodeManager管理时|健康、指标和故障恢复接口共用引擎业务端口，不另设管理端口|
|用户客户端|客户端IP|系统分配|Coordinator推理API|Kubernetes节点IP|默认NodePort 31015 → 进程1025|TCP|HTTP/HTTPS：推理请求|是|推理API Key可选，默认关闭；TLS服务端证书校验可选|默认无；可选TLS|数据面|MindIE Motor 3.2.0|通过Kubernetes NodePort访问推理服务时|NodePort配置：`motor_deploy_config.coordinator_infer_node_port`；进程端口配置：`api_config.coordinator_api_infer_port`|
|监控客户端|客户端IP|系统分配|Coordinator观测API|Kubernetes节点IP|默认NodePort 31017 → 进程1027|TCP|HTTP/HTTPS：`/metrics`、`/health`；`/instances`也由此端口提供|是|`/metrics`无API Key；`/instances`可选管理API Key；TLS服务端证书校验可选|默认无；可选TLS|观测面|MindIE Motor 3.2.0|通过Kubernetes NodePort访问Coordinator观测API时|NodePort配置：`motor_deploy_config.coordinator_obs_node_port`；进程端口配置：`api_config.coordinator_obs_port`；TLS使用`mgmt_tls_config`|
|监控客户端|客户端IP|系统分配|Controller观测API|Kubernetes节点IP|默认NodePort 31027 → 进程1027|TCP|HTTP/HTTPS：清单、告警和指标代理|是|默认无；TLS服务端证书校验可选|默认无；可选TLS|观测面|MindIE Motor 3.2.0|通过Kubernetes NodePort访问Controller观测API时|NodePort配置：`motor_deploy_config.controller_observability_node_port`；`/observability/metrics`已弃用，指标优先从Coordinator读取|

## 按功能启用的通信

以下端口不能作为所有部署的必开端口。Kubernetes Service模板中出现端口，也不代表所选后端进程一定监听该端口。

|源设备|源IP地址|源端口|目的设备|目的IP地址|目的端口（侦听）|协议|端口说明|侦听端口是否可更改|认证方式|加密方式|所属平面|版本|特殊场景|备注|
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
|Coordinator|所在Pod IP|系统分配|KV Conductor|Conductor服务地址|默认13333|TCP|HTTP：KV亲和的注册、注销、查询等请求|是|Motor内置服务未提供认证|无|数据面|MindIE Motor 3.2.0|启用KV亲和调度并部署KV Conductor时|HTTP端口配置：顶层`kv_conductor_config.http_server_port`|
|KV Conductor|所在Pod IP|系统分配|原生引擎KV事件发布端|引擎Pod IP|由`kv-events-config.endpoint`等配置决定|TCP|ZMQ SUB主动连接引擎PUB；事件数据从引擎流向Conductor；回放端口可另配|是|Motor内置链路未提供认证|无|数据面|MindIE Motor 3.2.0|引擎启用KV事件的ZMQ发布时|HBM事件和回放端口按各自配置的基础端口加DP秩解析；`5557`是示例值，需与引擎发布配置一致|
|原生引擎Worker|所在Pod IP|系统分配|KV Conductor|Conductor服务地址|默认13333|TCP|HTTP：向`/events`推送KV事件|是|Motor内置服务未提供认证|无|数据面|MindIE Motor 3.2.0|引擎配置为通过HTTP推送KV事件时|复用Conductor的HTTP服务端口，配置项为`kv_conductor_config.http_server_port`|
|KV Conductor|所在Pod IP|系统分配|KV池事件发布端|KV池服务地址|由`pool_endpoint`决定，例如5557|TCP|ZMQ SUB订阅Mooncake/MemCache等池化事件|是|由池化后端决定|由池化后端决定|数据面|MindIE Motor 3.2.0|Mooncake/MemCache池化后端发布KV事件，且配置`pool_endpoint`时|Conductor主动连接池化服务，池化服务向Conductor发送事件|
|原生引擎或独立KV Store|所在Pod IP|系统分配|Mooncake Master|Master服务地址|默认50088|TCP|Mooncake池化元数据及访问|是|由Mooncake配置决定|由Mooncake配置决定|数据面|MindIE Motor 3.2.0|启用Mooncake KV池化时|Master端口配置：`kv_cache_store_config.port`；`kv_cache_store_config.store_mode`为`standalone`时还由独立Store进程访问Master|
|MemCache客户端|所在Pod IP|系统分配|MemCache MetaService|MetaService服务地址|默认50089|TCP|MetaService配置存取|是|由MemCache配置决定|由MemCache配置决定|数据面|MindIE Motor 3.2.0|启用MemCache KV池化时|MetaService端口配置：`kv_cache_store_config.config_store_port`|
|Coordinator|所在Pod IP|系统分配|KV池指标服务|KV池服务地址|按后端配置，Mooncake示例为50090|TCP|HTTP：采集KV池`/metrics`|是|由池化后端决定|由池化后端决定|观测面|MindIE Motor 3.2.0|启用Mooncake/MemCache KV池化及指标采集时|配置`kv_cache_store_config`时自动启用采集；通过`prometheus_metrics_config`指定采集地址或端口，并与后端监听配置一致|
|Decode原生引擎|所在Pod IP|系统分配|Coordinator Worker metaserver|Coordinator Pod IP或配置的可达地址|默认12000 + Worker序号|TCP|HTTP：`POST /v1/metaserver`回调|是|无API Key|无TLS|内部接口|MindIE Motor 3.2.0|vLLM Layerwise/Trigger PD的Decode回调|基准端口非零时随Worker启动监听，`worker_metaserver_base_port=0`可关闭；通过Pod地址访问，未创建Kubernetes Service，需限制Pod网络访问|
|SGLang Decode引擎|所在Pod IP|系统分配|SGLang Prefill引擎bootstrap端|Prefill引擎Pod IP|由`disaggregation_bootstrap_port`配置|TCP|SGLang原生PD bootstrap连接|是|由SGLang配置决定|由SGLang配置决定|数据面|MindIE Motor 3.2.0|SGLang PD分离|监听端口由Prefill的`engine_config.disaggregation_bootstrap_port`配置，独立于业务端口；Coordinator将该地址传给Decode|
|Coordinator|所在Pod IP|系统分配|本Pod Render Sidecar|127.0.0.1或`::1`|默认8100|TCP|HTTP：Render/Derender及健康检查|是|无|无|内部接口|MindIE Motor 3.2.0|启用Render（`render_config.enable=true`）时|端口配置：`render_config.endpoint.port`；地址仅限本机（同Pod），无需跨Pod访问|
|Coordinator、原生引擎|所在Pod IP|系统分配|OTLP接收端|配置的接收端地址|由Tracing endpoint决定；常见HTTP 4318、gRPC 4317|TCP|导出Trace Span|是|由接收端配置决定|由接收端及URL配置决定|观测面|MindIE Motor 3.2.0|Coordinator或引擎开启Tracing并配置OTLP导出时|Coordinator使用`tracer_config.endpoint`，引擎使用`engine_config.otlp-traces-endpoint`；端口取自各自的导出URL|
