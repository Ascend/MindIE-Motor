# MindIE Motor Architecture

## Introduction to MindIE Motor

**MindIE Motor** is a request scheduling framework oriented to distributed inference of large language models (LLMs), such as **PD disaggregation inference** (separation of the Prefill and Decode phases). Through an open and scalable inference service platform architecture, it connects to [vLLM-Ascend](https://github.com/vllm-project/vllm-ascend) and the SGLang native runtime, aiming to meet the high-performance inference requirements of LLMs.

> [!NOTE]NOTE
> The original MindIE PyMotor repository has been renamed to MindIE Motor starting from version 3.1.0, and this naming will be used in subsequent releases. Its software positioning and core functionalities remain unchanged. It connects to the vLLM-Ascend and SGLang native engines.

### Core Capabilities

MindIE Motor primarily provides capabilities in the following two aspects:

- **PD disaggregation request scheduling**: distributes external customer requests to the Prefill/Decode instance with the lowest load, achieving **load balancing**.

- **RAS (Reliability, Availability and Serviceability)**: enhances the **reliability, availability, and serviceability** of the PD disaggregation service.

## System Architecture

The interaction architecture of MindIE Motor and its peripheral components is shown in the following figure:

**Figure 1 MindIE Motor architecture**

![Architecture diagram](./imgs/MindIE_Motor_Architecture.png)

## Key Components and Modules

The core components of MindIE Motor are defined as follows:

### Coordinator

As the **unified entry point** for user inference requests, it is responsible for receiving high-concurrency requests, performing request scheduling, management, and forwarding, serving as the data flow hub of the entire cluster.

- **Endpoint**: Provides RESTful APIs externally, including the data-plane OpenAI APIs and management-plane APIs such as health probes and metrics.

- **Router**: Provides request routing and forwarding capabilities.

- **Scheduler**: Load balancing scheduler.

- **RequestManager**: Request manager, responsible for global request information statistics and management.

- **InstanceManager**: Synchronizes instance health status, assists load balancing scheduling, and isolates faulty instances.

### Controller

As the **status manager and decision-making brain** of the cluster, it is responsible for global service status management and RAS capability decision-making.

- **FaultManager**: fault management module, which receives and handles reported faults, such as isolation, restart, and self-healing.

- **InsManager**: instance manager, which allocates and dynamically adjusts PD instance identities (Prefill or Decode).

- **CCAEReporter**: O&M management information reporter, which synchronizes instance status and metrics to O&M management platforms such as [CCAE](https://www.hiascend.com/software/ccae).

- **EventPusher**: event pusher, which synchronizes instance status information to the Coordinator.

### Deployer

Based on the reference scripts for Kubernetes inference **service deployment**, it provides capabilities such as service startup, shutdown, and elastic scaling.

- **Deploy**: one-click service startup and shutdown script tool.

- **Probe**: [health probe](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) configuration script.

- **LogCollector**: Kubernetes log collection script.

- **BootHelper**: container startup script that automatically configures environment variables.

### NodeManager

As the node-level service manager, it provides the following capabilities:

- **Node-level service process startup**: registers with the Controller, obtains an instance identity, and directly starts the native vLLM or SGLang process on the local node.

- **Node-level health status management**: monitors the status of inference service subprocesses and reports health status and heartbeats to the Controller.

### Peripheral Components

- **[vLLM-Ascend](https://github.com/vllm-project/vllm-ascend)**: native inference engine, which provides model instance acceleration capabilities. For the supported scope, see [Supported Inference Engines](./user_guide/features/supported_inference_engines.md).

- **[SGLang](https://github.com/sgl-project/sglang)**: native inference engine, which provides model instance acceleration capabilities. For the supported scope, see [Supported Inference Engines](./user_guide/features/supported_inference_engines.md).

- **[MindCluster](https://gitcode.com/Ascend/mind-cluster)**: Ascend cluster enablement component, which provides underlying Kubernetes support capabilities, CRD definitions in PD disaggregation, and supporting operators.

- (Optional) **[CCAE](https://www.hiascend.com/software/ccae)**: visualized O&M platform, which integrates computing, storage, and network resources.
