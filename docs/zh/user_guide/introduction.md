# 简介

## 概述

MindIE Motor是面向LLM PD分离推理的请求调度框架，通过开放、可扩展的推理服务化平台架构提供推理服务化能力，向下对接MindIE LLM，满足大语言模型高性能推理需求。MindIE Motor提供以下两个方面的能力：

- PD分离的请求调度：主要将外部的客户请求分发到负载最低的Prefill/Decode实例上，起到负载均衡的作用；
- RAS（Reliability、Availability和Serviceability）：增强PD分离服务可靠性、可用性和可服务性的能力。

## MindIE Motor架构图

MindIE Motor及其周边组件交互架构图如下所示。

**图 1** MindIE Motor架构图<a id="fig186975591945"></a>  
![](../figures/mindie_motor_architectural_diagram.png)

MindIE Motor提供PD分离推理服务化调度和RAS能力，关键组件和模块解释如下。

- Coordinator：调度器。是用户推理请求的入口，接收高并发的推理请求，进行请求调度、请求管理、请求转发等，是整个集群的数据请求入口。
    - Endpoint：对外RESTful接口，如OpenAI接口。
    - Metrics：PD分离服务整体的Metrics统计指标，是整个服务的Prefill/Decode实例的统计指标汇总。
    - Controller Monitor：接收Controller同步的实例状态信息，如健康状态、故障实例等信息。
    - LoadBalancer：负载均衡调度。
    - RequestMonitor：请求状态监控，如请求阶段、请求异常等。

- Controller：控制器。完成集群内所有Prefill/Decode实例的业务状态管控、PD身份管理与决策、RAS能力等，是整个集群的状态管控器和决策大脑。
    - FaultManager：故障管理模块，接收上报的故障，并处理故障，如隔离、重启、自愈恢复等。
    - InsManager：实例管理器，负责PD实例身份的分配、调整等。
    - CCAEReporter：运维管理信息上报，上报PD实例、Metrics等统计信息。
    - InsMonitor：PD实例监控，包括心跳、负载等。
- MindIE LLM：提供单个模型服务实例（Prefiller/Decoder）服务化推理能力，提供ContinuousBatching、PagedAttention、投机推理等LLM加速特性。
- ClusterD：MindCluster高阶组件，负责故障诊断和全局RankTable表（整个PD分离服务所需的组网和Device信息）下发等功能。
- CCAE：算存网一体化运维可视化平台。
