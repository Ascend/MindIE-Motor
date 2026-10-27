# Introduction

This project implements a large-scale EP deployment solution for GLM5 series models on the ModelArts platform. It supports inference service-level KV cache pooling, in-EP-instance affinity scheduling (at DP domain granularity), and EP instance-level fault recovery, ensuring a high-performance and highly reliable LLM inference service.

## Key Features

- **Deployment form**: 8-node large-scale EP cluster of Atlas 800I A2s

- **Instance gateway**: MindIE Motor
  - Supports node KV cache affinity scheduling
  - Load balancing scheduling

- **KV Cache multi-level caching**: yuanrong
  - Supports L2 (DDR) KV cache caching
  - Implements cross-large-EP cache pooling through a global ETCD

- **Reliability and availability**
  - Automatic deployment on ModelArts
  - Large-scale EP instance-level rescheduling and recovery

## File Directory

```txt
.
├── ModelArts_GLM5_yuanrong_Motor_8Node_BigEP_Deployment_Guide.md # Solution deployment and test guide
├── auto_deployment_scripts/ # MA automated deployment script code
│   ├── start.sh # MA unified startup script
│   ├── Motor/ # Motor startup and deployment script
│   │   ├── prepare.sh
│   │   └── start_motor.sh
│   ├── yuanrong/ # Yuanrong startup and deployment script
│   │   ├── start_base_yr.sh
│   │   ├── start_etcd.sh
│   │   └── start_yr_worker.sh
│   ├── etcd/ # etcd pooling startup script
│   │   └── etcd-all.yaml
│   ├── health_check/ # MA health check scripts (readiness probe and liveness probe)
│   │   ├── vllm_probe.py
│   │   ├── vllm_probe_yr.py
│   └── └── utils.sh
└── vLLM_performance_test_scripts/ # Performance and long-stability automated test project
    └── vLLM_bench_template.sh
```
