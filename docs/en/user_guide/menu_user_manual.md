# MindIE Motor User Guide

- [Introduction to MindIE Motor](../architecture.md)

- [Preparing the Basic Environment](./environment_preparation.md)

- [Preparing the MindIE Motor Image](./maintenance/build_motor_image_from_vllm_ascend.md)

- [Service Deployment]()

  - [Service Deployment Methods](./deployment/k8s/README.md)

  - [PD Co-location Deployment](./deployment/k8s/pd_aggregation_deployment.md)

  - [K8s-based Single-Container PD Disaggregation Deployment](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/single_container/README.md)

  - [K8s-based Multi-Container PD Disaggregation Deployment](./deployment/k8s/pd_disaggregation_deployment.md)

  - [Single-Container Docker-only PD Disaggregation Deployment](./deployment/docker/single_container.md)

  - [Multi-container Docker-only PD Disaggregation Deployment](./deployment/docker/multi_container.md)

  - [Configuration Parameter Description]()

    - [user_config.json Configuration File Reference](./configuration/config_reference.md)

    - [Hot Update Configuration Item Description](./configuration/update_config_whitelist.md)

- [Feature Description]()

  - [Supported Inference Engines](./features/supported_inference_engines.md)

  - [EPD Disaggregation](./features/EPD_disaggregation.md)

  - [PD Disaggregation](./features/pd_disaggregation.md)

  - [KV Cache Affinity Scheduling Capability Deployment](./features/KV_cache_affinity.md)

  - [KV Pooling Capability Deployment](./features/kv_cache_store/README.md)

  - [Automatic Elastic Scaling](./features/auto_scaling.md)

  - [Manual Scaling](./features/manual_scaling.md)

  - [Active/Standby Switchover](./features/fault_tolerance/standby.md)

  - [Tracing Deployment](./features/tracing.md)

  - [ScaleP2D Fault Recovery](./features/fault_tolerance/scale_p2d.md)

  - [Container Snapshot](./features/container_snapshot.md)

  - [Simulated Inference Health Probe](./features/sim_inference.md)

  - [Rescheduling in Fault Scenarios](./features/fault_tolerance/rescheduler.md)

  - [vLLM Deployment Script Conversion Tool](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md)

- [API Reference]()

  - [Interface Description](./api/README.md)

  - [User-side Interfaces](./api/service_interfaces.md)

  - [Management Interfaces](./api/management_interfaces.md)

  - [Metrics Interfaces](./api/metrics_interfaces.md)

- [Cluster Maintenance]()

  - [Common Maintenance](./maintenance/maintenance_tips.md)

  - [Fault Cases](./maintenance/solutions_to_common_problems.md)

- [Cluster Management Components]()

  - [Controller](../developer_guide/components/controller.md)

  - [Coordinator](../developer_guide/components/coordinator.md)

  - [Engine Server](../developer_guide/components/engine_server.md)

  - [Node Manager](../developer_guide/components/node_manager.md)
