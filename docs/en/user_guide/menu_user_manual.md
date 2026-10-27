# MindIE Motor User Guide

- [Introduction to MindIE Motor](../architecture.md)

- [Preparing the Basic Environment](./environment_preparation.md)

- [Preparing the MindIE Motor Image](./maintenance/build_motor_image_from_vllm_ascend.md)

- [Service Deployment]()

  - [K8s-based vLLM PD Aggregation Deployment](./deployment/k8s/pd_aggregation_deployment.md)

  - [K8s-based vLLM PD Disaggregation Deployment](./deployment/k8s/pd_disaggregation_deployment.md)

  - [K8s-based SGLang PD Disaggregation Deployment](./deployment/k8s/pd_disaggregation_sglang.md)

  - [Single-Container Docker-only PD Disaggregation Deployment](./deployment/docker/single_container.md)

  - [Multi-Container Docker-only PD Disaggregation Deployment](./deployment/docker/multi_container.md)

  - [Slurm Deployment]()

    - [Environment Preparation](./deployment/slurm/environment_preparation.md)

    - [Service Deployment](./deployment/slurm/service_deployment.md)

  - [Standalone Coordinator Deployment](./deployment/standalone.md)

  - [Deployer Deployment Configuration Reference](./deployment/k8s/README.md)

  - [Configuration Parameter Description]()

    - [user_config.json Configuration File Reference](./configuration/config_reference.md)

    - [Hot Update Configuration Item Description](./configuration/update_config_whitelist.md)

- [Feature Description]()

  - [Feature Support List](./features/supported_features.md)

  - [Supported Inference Engines](./features/supported_inference_engines.md)

  - [EPD Disaggregation](./features/EPD_disaggregation.md)

  - [PD Disaggregation](./features/pd_disaggregation.md)

  - [Dynamic Bucket Scheduling](./features/dynamic_bucket.md)

  - [KV Cache Affinity Scheduling Capability Deployment](./features/kvcache_affinity.md)

  - [KV Pooling Capability Deployment](./features/kv_cache_store/README.md)

  - [max_tokens Adaptation](./features/max_tokens_adaptation.md)

  - [Automatic Elastic Scaling](./features/auto_scaling.md)

  - [Manual Scaling](./features/manual_scaling.md)

  - [Active/Standby Switchover](./features/fault_tolerance/standby.md)

  - [Tracing Deployment](./features/tracing.md)

  - [ScaleP2D Fault Recovery](./features/fault_tolerance/scale_p2d.md)

  - [Container Snapshot](./features/container_snapshot.md)

  - [D2D Weight Loading](./features/startup_acceleration.md)

  - [Virtual Inference Health Probe](./features/sim_inference.md)

  - [Instance Rescheduling](./features/fault_tolerance/instance_rescheduling.md)

  - [Request Rescheduling](./features/fault_tolerance/rescheduler.md)

  - [Service Rate Limiting](./features/rate_limiting.md)

  - [Precision Detection](./features/precision_detection.md)

  - [Data Obfuscation Inference (PMCC)](./features/data_obfuscation.md)

  - [vLLM Deployment Script Conversion Tool](../../../model_configs/vllm/models/README_EN.md)

- [API Reference]()

  - [Interface Description](./api/README.md)

  - [User-side Interfaces](./api/service_interfaces.md)

  - [Management Interfaces](./api/management_interfaces.md)

  - [Metrics Interfaces](./api/metrics_interfaces.md)

  - [Service Running Status Observation Interface](./api/observability_interface.md)

- [Cluster Maintenance]()

  - [Common Maintenance](./maintenance/maintenance_tips.md)

  - [Fault Cases](./maintenance/solutions_to_common_problems.md)

- [Cluster Management Components]()

  - [Controller](../developer_guide/components/controller.md)

  - [Coordinator](../developer_guide/components/coordinator.md)

  - [Node Manager](../developer_guide/components/node_manager.md)

- [Error Code Reference]()

  - [Usage Instructions](./error_code/usage_instruction.md)

  - [MindIE Motor Error Code Reference](./error_code/error_code_reference.md)
