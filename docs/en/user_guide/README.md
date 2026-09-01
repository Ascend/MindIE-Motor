# Introduction

Motor provides one-click PD disaggregation and PD co-location deployment. Based on a cloud-native plugin-based architecture, it flexibly adapts to multiple inference engines ([vLLM](https://github.com/vllm-project/vllm-ascend) and [SGLang](https://github.com/sgl-project/sglang)), and combines high-performance scheduling and load balancing capabilities to build highly available, scalable large-scale inference services.

## Quick Start

**Environment preparation**: For the software and hardware environment preparation and installation steps before installation, see [Environment Preparation](./environment_preparation.md).

**Quick deployment**: To quickly experience the full process of starting the service, calling APIs, testing accuracy and performance, and stopping the service, see [Quick Deployment](./quick_start.md).

**Best practices**: For PD disaggregation deployment, see [detailed guide for PD disaggregation deployment](./deployment/k8s/pd_disaggregation_deployment.md). For PD co-location deployment, see [detailed guide for PD co-location deployment](./deployment/k8s/pd_aggregation_deployment.md).
