# MindIE Motor

## 🔥Latest News

[2025/12] MindIE Motor正式开源。

## 🚀简介

MindIE Motor是面向通用模型场景的推理服务化框架，通过开放、可扩展的推理服务化平台架构提供推理服务化能力，支持对接业界主流推理框架接口，满足大语言模型的高性能推理需求。

MindIE Motor的组件包括MindIE Service Tools、集群管理组件（Deployer、Controller和Coordinator），通过对接昇腾推理加速引擎带来大模型在昇腾环境中的性能提升，并逐渐以高性能和易用性牵引用户向MindIE原生推理服务化框架迁移。其架构图如[图1 MindIE-Motor架构图](#fig186975591945)所示。

**图 1** MindIE Motor架构图<a id="fig186975591945"></a>  
![](./docs/zh/figures/mindie_motor_architectural_diagram.png)

-   MindIE Motor提供推理服务化部署和运维能力。
    -   MindIE Service Tools：昇腾推理服务化工具；主要功能有大模型推理性能测试、精度测试、可视化等能力，并且支持通过配置提升吞吐。
    -   Deployer：部署器，底层集成Kubernetes（简称K8s）生态，主要支持对Server服务集群的一键式部署管理。
    -   Controller：控制器，完成集群内所有Server的业务状态管控、PD身份管理与决策、资源管理决策等，是整个集群的状态管控器和决策大脑。
    -   Coordinator：调度器，是用户推理请求的入口，接收高并发的推理请求，进行请求调度、请求管理、请求转发等，是整个集群的数据请求入口。
    -   MindIE Backends：支持昇腾MindIE LLM后端。

-   MindIE LLM：提供大模型推理能力，同时提供多并发请求的调度功能。



## 🔍目录结构

*待开发补充*

## ⚡️版本说明

|MindIE软件版本|CANN版本兼容性|
|:---|:---|
|2.2.RC1|8.3.RC2|

## ⚡️环境部署

MindIE Motor安装前的相关软硬件环境准备，以及安装步骤，请参见[安装指南](./docs/zh/User_Guide/installation_guide.md)。


## ⚡️快速入门

  快速体验启动服务、接口调用、精度&性能测试和停止服务全流程，请参见[快速入门](./docs/zh/User_Guide/quick_start.md)。

## 📝学习文档

- [集群服务部署](./docs/zh/user_guide/service_deployment/)：介绍MindIE Motor集群服务部署方式，包括单机（非分布式）服务部署和PD分离单、多机服务部署。
- [集群管理组件](./docs/zh/user_guide/cluster_management_component/)：介绍MindIE Motor集群管理组件，包括Deployer、Controller和Coordinator。
- [服务化接口](./docs/zh/user_guide/service_oriented_interface/)：介绍MindIE Motor提供的用户侧接口和集群内通信接口。
- [配套工具](./docs/zh/user_guide/service_oriented_optimization_tool.md)：介绍MindIE Motor提供的配套工具，包括性能/精度测试工具、MindIE探针工具、服务化调优工具、CertTools、OM Adapter和Node Manager。

## 📝免责声明

版权所有© 2025 MindIE Project.

您对"本文档"的复制、使用、修改及分发受知识共享（Creative Commmons）署名——相同方式共享4.0国际公共许可协议（以下简称"CC BY-SA 4.0"）的约束。为了方便用户理解，您可以通过访问https://creativecommons.org/licenses/by-sa/4.0/了解CC BY-SA 4.0的概要（但不是替代）。CC BY-SA 4.0的完整协议内容您可以访问如下网址获取：https://creativecommons.org/licenses/by-sa/4.0/legalcode。

## 📝贡献指南

1. 提交错误报错：如果您在MindIE Motor中发现了一个不存在安全问题的漏洞，请在MindIE Motor仓库中的Issues中搜索，以防该漏洞被重复提交，如果找不到漏洞可以创建一个新的Issues。如果发现了一个安全问题请不要将其公开，请参阅安全问题处理方式。提交错误报告时应该包含完整信息。
2. 安全问题处理：本项目中对安全问题处理的形式，请通过邮箱通知项目核心人员确认编辑。
3. 解决现有问题：通过查看仓库的Issues列表可以发现需要处理的问题信息，可以尝试解决其中的某个问题。
4. 如何提供新功能：请使用Issues的Feature标签进行标记，我们会定期处理和确认开发。
5. 开始贡献：
   1. Fork本项目的仓库。
   2. Clone到本地。
   3. 创建开发分支。
   4. 本地测试：提交前请通过所有单元测试，包括新增的测试用例。
   5. 提交代码。
   6. 新建Pull Request。
   7. 代码检视：您需要根据评审意见修改代码，并重新提交更新。此流程可能涉及多轮迭代。
   8. 当您的PR获得足够数量的检视者批准后，Committer会进行最终审核。
   9. 审核和测试通过后，CI会将您的PR合并到项目的主干分支。
 
更多贡献相关文档请参见[贡献指南](./contributing.md)。

## 📝相关信息

- [安全声明](./security.md)
- [LICENSE](./LICENSE.md)