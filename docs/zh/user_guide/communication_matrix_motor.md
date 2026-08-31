# MindIE Motor通信矩阵

提供了MindIE Motor的通信矩阵，包括产品开放的端口、该端口使用的传输层协议、通过该端口与对端通信的通信网元名称、认证方式、用途等信息说明。

>[!NOTE]说明
>datadist、HCCL、LCCL和ATB相关通信矩阵请参考[CANN 通信矩阵](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/latest/maintenref/refdoc/refer001.html)。

|源设备|源IP地址|源端口|目的设备|目的IP地址|目的端口（侦听）|协议|端口说明|侦听端口是否可更改|认证方式|加密方式|所属平面|版本|特殊场景|备注|
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
|MindIE集群服务调度器（内部NodeManager）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部Controller）|MindIE集群服务调度器Pod IP|默认1026|TCP|实例注册、重注册、心跳上报|是|证书认证，可配置，默认关闭|TLS1.3|内部接口|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部Coordinator）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部Controller）|MindIE集群服务调度器Pod IP|默认1026|TCP|告警上报|是|证书认证，可配置，默认关闭|TLS1.3|内部接口|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部Controller）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部Coordinator）|MindIE集群服务调度器Pod IP|1026|TCP|实例推送、心跳探活|是|证书认证，可配置，默认关闭|TLS1.3|内部接口|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部Controller）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部Coordinator obs）|MindIE集群服务调度器Pod IP|默认1027|TCP|拉取Metrics指标数据|是|证书认证，可配置，默认关闭|TLS1.3|内部接口|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部Coordinator、Controller）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部ETCD）|MindIE集群服务调度器Pod IP|默认2379|TCP|主备选举、租约管理、实例状态等|是|证书认证，可配置，默认关闭|TLS1.3|数据面|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部Coordinator）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部EngineServer）|MindIE集群服务调度器Pod IP|动态分配，端口规则：10000+2*dp Rank ID|TCP|内部P实例或D实例的推理端口|是|证书认证，可配置，默认关闭|TLS1.3|数据面|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部Controller）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部EngineServer）|MindIE集群服务调度器Pod IP|动态分配，端口规则：10001+2*dp Rank ID|TCP|内部P实例或D实例的管理端口，包含状态查询、Metrics|是|证书认证，可配置，默认关闭|TLS1.3|数据面|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部Coordinator）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部Conductor）|MindIE集群服务调度器Pod IP|默认13333|TCP|开启亲和性调度特性后提供注册、注销、查询等服务|是|无|无|数据面|MindIE 3.1.0|无|无|
|MindIE集群服务调度器（内部EngineServer）|MindIE集群服务调度器Pod IP|随机端口（由操作系统自动分配）|MindIE集群服务调度器（内部Mooncake Master）|MindIE集群服务调度器Pod IP|默认50088|TCP|开启池化特性后提供KV Cache的注册、访问等服务|是|无|无|数据面|MindIE 3.1.0|无|无|
|用户客户端|客户端通信IP地址|随机端口（由操作系统自动分配），默认范围32768~60999。|MindIE Motor服务端|Host IP|默认端口31015，用户可配置|TCP|推理端口|是|证书认证，可配置，默认关闭|TLS1.3|数据面|MindIE 3.1.0|无|无|
|用户客户端|客户端通信IP地址|随机端口（由操作系统自动分配），默认范围32768~60999。|MindIE Motor服务端|Host IP|默认端口31017，用户可配置|TCP|Coordinator侧的原始引擎指标和健康检测|是|证书认证，可配置，默认关闭|TLS1.3|数据面|MindIE 3.1.0|无|无|
|用户客户端|客户端通信IP地址|随机端口（由操作系统自动分配），默认范围32768~60999。|MindIE Motor服务端|Host IP|默认端口31027，用户可配置|TCP|Controller侧汇聚后的运维观测数据（清单+指标+告警），供外部监控平台（如Prometheus、CCAE Reporter）使用。|是|证书认证，可配置，默认关闭|TLS1.3|数据面|MindIE 3.1.0|无|无|
