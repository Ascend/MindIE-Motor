# 简介

本项目基于ModelArts平台，实现GLM5系列模型的大EP部署方案。支持推理服务级KV cache池化缓存、EP实例内亲和调度(DP域粒度)以及EP实例级故障恢复机制，确保高性能和高可靠性的大模型推理服务。

## 主要特性

- **部署形态**：8机A2大EP集群
- **实例网关**：MindIE-Motor
  - 支持节点KV cache亲和性调度
  - 负载均衡调度
- **KV Cache多级缓存**：yuanrong
  - 支持L2级(DDR)KV cache缓存
  - 通过全局ETCD实现跨大EP缓存池化
- **可靠可用性**
  - ModelArts 上自动部署
  - 大EP实例级重调度恢复

# 文件目录

```txt
.
├── ModelArts_GLM5_yuanrong_PyMotor_8Node_BigEP_Deployment_Guide.md #方案部署测试指导文档
├── auto_deployment_scripts/ #MA自动化部署脚本代码
│   ├── start.sh #MA统一启动脚本
│   ├── PyMotor/ #PyMotor启动部署脚本
│   │   ├── prepare.sh
│   │   └── start_motor.sh
│   ├── yuanrong/ #元戎启动部署脚本
│   │   ├── start_base_yr.sh
│   │   ├── start_etcd.sh
│   │   └── start_yr_worker.sh
│   ├── etcd/ #etcd池化启动脚本
│   │   └── etcd-all.yaml
│   ├── health_check/ #MA健康检查脚本(就绪探针、存活探针)
│   │   ├── vllm_probe.py
│   │   ├── vllm_probe_yr.py
│   └── └── utils.sh
└── vLLM_performance_test_scripts/ #性能、长稳自动化测试工程
    └── vLLM_bench_template.sh
```
