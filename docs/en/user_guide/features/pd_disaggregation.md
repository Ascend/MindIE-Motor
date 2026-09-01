# PD Disaggregation Description

## What Is PD Disaggregation

**PD disaggregation** (Prefill & Decode disaggregation) splits the two stages of large language model inference, Prefill and Decode, onto different instances for execution. It is suitable for scenarios with high latency and throughput requirements. PD disaggregation improves NPU utilization, reduces the mutual interference caused by time-sharing between Prefill and Decode, and increases overall throughput at the same latency.

The two inference stages are described as follows:

- **Prefill stage**: Performs a complete forward propagation on the input prompt to generate the initial hidden states. It is **compute-intensive**. Prefill must be executed once for each new input sequence.

- **Decode stage**: Generates subsequent tokens step by step based on the Prefill results. Each step computes only the activation and attention of the latest token, so the per-step computation is small, but it must be executed repeatedly until generation ends. It is **memory-intensive** (dominated by memory access such as KV Cache).

This repository adopts a **multi-node PD disaggregation** deployment solution: a K8s Service exposes the inference entry point for the Coordinator, and multiple Deployments are used to deploy the Controller (single Pod), the Coordinator (single Pod), and the Server (several Pods each for P instances and D instances). The Controller is responsible for cluster and instance management, the Coordinator receives user requests and schedules them to P/D instances, and the P instances and D instances collaboratively complete a full inference.

## What Are the Main Advantages of PD Disaggregation

- **Better resource utilization**: Prefill is compute-intensive while Decode is memory-intensive. Due to their different characteristics, disaggregated deployment can make fuller use of the NPU's compute and bandwidth resources.

- **Higher throughput**: While Prefill processes new requests, Decode can continuously decode existing requests, resulting in higher overall processing capacity.

- **More controllable latency**: Separating the two phases reduces queuing and waiting, which helps lower latency, especially in high-concurrency scenarios.
