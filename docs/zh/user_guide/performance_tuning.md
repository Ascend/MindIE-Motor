# 大规模专家并行方案典型配置

## 概述

专家指 MoE 模型中的子网络（Expert），每个专家是相对独立的前馈网络，由门控根据输入选择部分专家参与计算；**大规模专家**指专家数量较多的 MoE 模型（如专家数达数十至上百），模型总参数量大、单次激活的专家数远小于总专家数；**大规模专家并行**指将众多专家分布到多台计算设备上，通过并行执行被选中的专家计算以完成推理或训练。大规模部署时，专家被切分并部署在多台计算设备上，因此激活某专家即触发承载该专家的设备上的计算，并在执行过程中进行必要的通信与调度。MindIE Motor 通过 P（Prefill）与 D（Decode）实例分工及集群管理组件（Coordinator/Controller）完成调度与资源管理，详见[集群管理组件](./cluster_management_component/coordinator.md)与[PD 分离服务部署](./service_deployment/pd_separation_service_deployment.md)。

建议按以下流程开展性能调优：

1. **分析**：采集时延、吞吐、NPU 利用率等指标，确定性能瓶颈。
2. **定位**：区分问题位于算力、通信、内存或 P/D 配比等环节。
3. **优化**：结合下文典型配置，调整 P/D 实例数、节点数等参数。
4. **验证**：复测关键指标并与基线对比，确认优化效果。

<img src="https://raw.gitcode.com/user-images/assets/8772838/dcc44dcc-eeb0-49b7-9284-618e265192af/image.png" alt="性能调优流程图" style="max-width: 100%; width: 900px;" />

---

## 前提条件

进行大规模专家并行性能调优前，需确认以下条件已满足：

1. 支持的硬件

   - Atlas 800I A2 推理服务器的硬件与操作系统支持见[安装指南](./installation_guide.md)，PD 分离部署与特性支持度见[集群管理组件概述](./cluster_management_component/general_comments.md)与[PD 分离服务部署](./service_deployment/pd_separation_service_deployment.md)。
   - Atlas 800I A3 超节点服务器的硬件与操作系统支持见[安装指南](./installation_guide.md)，PD 分离部署与特性支持度见[集群管理组件概述](./cluster_management_component/general_comments.md)与[PD 分离服务部署](./service_deployment/pd_separation_service_deployment.md)。
2. 环境检查

   - 部署与调优前已完成环境检查，具体检查项如下：
   - 网络拓扑：节点间链路、带宽与时延满足部署与集合通信要求，无丢包、误码等异常。
   - 存储性能：模型加载路径的 IOPS 与带宽满足大模型加载与运行要求。
   - NPU 配置：型号、片上内存、CANN/驱动版本、网口与光模块规格满足部署要求。
   - 支持的操作系统：详情请参见[安装指南](./installation_guide.md)。
3. 部署准备

   - 进行调优前，请参见[集群管理组件概述](./cluster_management_component/general_comments.md)查看 PD 分离特性支持度，并已参考[PD 分离服务部署](./service_deployment/pd_separation_service_deployment.md)完成 PD 分离部署。PD 分离指 Prefill 与 Decode 实例分离部署，协同完成推理。

---

## Atlas 800I A2服务器

大规模专家并行方案典型配置如表所示，按照当前规格，每个P实例分配2个计算节点，每个D实例支持分配4/8个计算节点。其中，P实例表示 Prefill 计算实例，D实例表示 Decode 计算实例。
每套集群管理组件实例（即 Coordinator/Controller 实例）管理一套PD实例，每套集群管理组件实例最大支持管理96节点（24P+6D）。
以192节点集群为例，需划分为两套独立的24P+6D实例，集群管理组件实例也需要对应创建两套，如所示，两套集群管理组件实例可共部署在一台或主备通算节点，如果现场部署集群管理组件实例的通算节点为双机，则集群管理组件实例也可以创建对应的主从实例（主从集群管理组件 Coordinator实例和主从集群管理组件 Controller实例）。其中，通算节点用于部署集群管理组件（Coordinator/Controller），不承担模型主体推理计算。

![1.png](https://raw.gitcode.com/user-images/assets/8772838/bb7fd4cf-5db5-437e-ace6-1b8bd3a35892/1.png '1.png')

 集群管理组件实例管理的PD实例规模支持按需灵活设置，如现场为64计算节点时，可以为64节点创建一套集群管理组件实例，也可以按照每16节点为一套独立的PD实例创建4套集群管理组件实例。

**表1 Atlas 800I A2 推理服务器典型配置**

|AI计算节点规模  |PD实例规格  |集群管理组件实例规格  |通算节点规格  |
|--|--|--|--|
| 8节点 | 2P+1D，每个P实例分配2个计算节点，每个D实例分配4个计算节点，另外出于可靠性考虑，可以配置1冗余节点。也可以在8节点+1冗余节点的基础上额外再增加双机，更加提升可靠性 |1*(集群管理组件 Coordinator+集群管理组件 Controller)  |  单机或双机|
|  16节点| 4P+1D，每个P实例分配2个计算节点，每个D实例分配8个计算节点，另外出于可靠性考虑，可以配置1冗余节点。也可以组成4P+2D组网，每个D实例分配4个计算节点，更加提升可靠性。 |1*(集群管理组件 Coordinator+集群管理组件 Controller)  |  单机或双机|
| 32节点 | 8P+2D，每个P实例分配2个计算节点，每个D实例分配8个计算节点。 |1*(集群管理组件 Coordinator+集群管理组件 Controller)  |  单机或双机|
|64节点 |16P+4D，每个P实例分配2个计算节点，每个D实例分配8个计算节点。  |1*(集群管理组件 Coordinator+集群管理组件 Controller)  |  单机或双机|
|96节点  | 24P+6D，每个P实例分配2个计算节点，每个D实例分配8个计算节点。 |1*(集群管理组件 Coordinator+集群管理组件 Controller)  |  单机或双机|
| N * 96节点 | N*(24P+6D)，每个P实例分配2个计算节点，每个D实例分配8个计算节点。 |1*(集群管理组件 Coordinator+集群管理组件 Controller)  | 单机或双机（大规模场景下要考虑根据集群管理组件实例数量来确定通算节点数量） |

>[!NOTE]须知
>大规模专家并行方案采用的Atlas 800I A2仅支持Atlas 800I A2 HCCS款，且NPU片上内存必须为64G，NPU网口光模块必须为200G。
>其中，HCCS款可理解为Atlas 800I A2 推理服务器中具备 HCCS 高速互联能力的款型，本方案仅适用于满足该互联规格的设备。

通算节点硬件典型配置如表2所示，当前通算节点CPU架构仅支持Arm架构。

**表2 Atlas 800I A2 推理服务器通算节点硬件要求**
| 型号 |典型配置  | 最低要求 |
|--|--|--|
| CPU |2 * 48核/2.6GHz  | 按每个Controller实例4核/Coordinator实例16核计算，一套Controller+Coordinator实例共需要20核。 |
| 内存 | 8 * 32GB | 8GB |
| 硬盘 | 12 * 1920GB SSD SATA 6Gb/s | 至少2个25GE网口 |
| 电源 | 2 * 900W交流电源 | - |
|RAID卡  |  4G Cache，支持RAID 50| - |

## Atlas 800I A3超节点服务器

大规模专家并行方案典型配置如表3-1所示，按照当前规格，每个P实例分配1个计算节点，每个D实例可分配1/2/4/8个计算节点。其中，P实例表示 Prefill 计算实例，D实例表示 Decode 计算实例。
每套集群管理组件实例（即 Coordinator/Controller 实例）管理一套独立的PD实例，每套集群管理组件实例管理一个超节点（48计算节点）。
如现场部署了多个超节点，需划分多套独立的PD实例（每套48节点），则集群管理组件实例也需要对应创建多套，组网示例如图3-5所示，多套集群管理组件实例可共部署在一台或两台通算节点，如果现场部署集群管理组件实例的通算节点为双机，则集群管理组件实例也可以创建对应的主从实例（主从Coordinator实例和主从Controller实例）。其中，通算节点用于部署集群管理组件（Coordinator/Controller），不承担模型主体推理计算。


图3-5 N个超节点组网

<img src="https://raw.gitcode.com/user-images/assets/8811849/6b7ed46b-a4e2-4fef-8572-3c35fd1bdd45/image.png" alt="图3-5 N个超节点组网" style="max-width: 100%; width: 900px;" />

集群管理组件实例管理的PD实例规模可按需灵活设置，如现场为48计算节点时，可以为48节点创建一套集群管理组件实例，也可以按照每16节点为一套独立的PD实例创建3套集群管理组件实例。

**表3 Atlas 800I A3 超节点服务器典型配置**

|计算节点规模  | PD实例规格 | 集群管理组件实例规格 |通算节点规格  |
|--|--|--|--|
| 2节点 | 1P+1D，每个P实例分配1个计算节点，每个D实例分配1个计算节点 |1*(集群管理组件 Coordinator+集群管理组件 Controller) |单机或主备双机  |
| 4节点 | 2P+1D，每个P实例分配1个计算节点，每个D实例分配2个计算节点 |1*(集群管理组件 Coordinator+集群管理组件 Controller) |单机或主备双机  |
| 8节点 |性能优先：4P+1D，每个P实例分配1个计算节点，每个D实例分配4个计算节点，可再配置一个节点作为冗余节点。可靠性优先：4P+2D，每个P实例分配1个计算节点，每个D实例分配2个计算节点，可再配置一个节点作为冗余节点。  | 1*(集群管理组件 Coordinator+集群管理组件 Controller) |单机或主备双机  |
| 16节点 | 性能优先：8P+1D，每个P实例分配1个计算节点，每个D实例分配8个计算节点，可再配置一个节点作为冗余节点。可靠性优先：8P+2D，每个P实例分配1个计算节点，每个D实例分配4个计算节点，可再配置一个节点作为冗余节点。 |1*(集群管理组件 Coordinator+集群管理组件 Controller) |单机或主备双机  |
|32节点  | 16P+4D，每个P实例分配1个计算节点，每个D实例分配4个计算节点，可再配置一个节点作为冗余节点。 |1*(集群管理组件 Coordinator+集群管理组件 Controller) |单机或主备双机  |
| 48节点 | 24P+6D，每个P实例分配1个计算节点，每个D实例分配4个计算节点。 | 1*(集群管理组件 Coordinator+集群管理组件 Controller) |单机或主备双机  |

## 验证调优结果与其它优化手段

1. **如何验证配置后性能得到优化？**

   - 业务指标：在相同负载下使用 [AISBench](https://gitee.com/aisbench/benchmark) 或自有压测工具，对比调优前后的 TTFT（首 Token 时延）、吞吐（Output Token Throughput）、端到端时延等，确认有提升且无劣化。
   - 剖析数据：使用 **msprof** 或 MindIE 提供的 **msServiceProfiler 服务化调优工具** 采集调优后的性能数据（含算子级与调度链路），解析后通过 MindStudio Insight 等工具做可视化分析，确认瓶颈是否缓解、计算/通信占比是否更合理。
   - 工具使用与配置详见[性能/精度测试工具](./service_oriented_optimization_tool.md#性能精度测试工具)与[服务化调优工具](./service_oriented_optimization_tool.md#服务化调优工具)。

2. **除参数配置外还有哪些性能优化方法？**

   - 算子/图优化：融合、选核等，依赖推理引擎与 CANN。
   - 通信优化：拓扑、流控、集合通信算法与重叠。
   - 运行参数优化：批大小（batch size）与序列长度、并发度的合理设置。
   - 量化与精度策略：如 W8A8 等，在满足精度前提下进行选用。
   - 环境一致性优化：驱动与 CANN 版本、固件与 BIOS 设置等。
   - 具体可结合 msprof 或 msServiceProfiler 的剖析结果做针对性优化，并再次用上述验证方法确认效果。