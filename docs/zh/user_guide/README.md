# 简介

Motor提供一键式 PD 分离与 PD 混部部署，基于云原生插件化架构灵活适配多种推理引擎（[vLLM](https://github.com/vllm-project/vllm-ascend)、[SGLang](https://github.com/sgl-project/sglang)），结合高性能调度与负载均衡能力，构建高可用、可扩展的大规模推理服务。

# 快速开始

**环境准备**：安装前的相关软硬件环境准备，以及安装步骤，请参见[环境准备](./environment_preparation.md)。

**快速部署**：快速体验启动服务、接口调用、精度&性能测试和停止服务全流程，请参见[快速部署](./quick_start.md)。

**最佳实践**：PD 分离部署请参见[PD 分离服务部署详细指导](./deployment/k8s/pd_disaggregation_deployment.md)，PD 混部部署请参见[PD 混部服务部署详细指导](./deployment/k8s/pd_aggregation_deployment.md)。
