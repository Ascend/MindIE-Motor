# [2026-09-28] 同机 Pod 的 IPv6 地址重复

- **现象 (Symptom)**：A5 IPv6 单栈上，同一节点的多个 Pod（如 Controller 与 Prefill）拿到同一个 IPv6。
- **根因 (Root cause)**：host-nic overlay 打开时给引擎设置了 `hostNetwork`，并把 `HCCL_IF_IP` 设成节点地址。hostNetwork 下 Pod IP 就是节点地址，同机 Pod 共用这一个 IPv6。
- **为什么会写出 (Why)**：把 UB 设备只能在 host netns 里用，当成 IPv6 单栈也必须 hostNetwork。
- **修复 (Fix)**：overlay 打开时引擎保持 Pod 网络，使用 CNI 分配的地址。去掉 `hostNetwork` 和 `HCCL_IF_IP=status.hostIP`。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_a5_engine_pod_config.py` 断言 overlay 下不设置 hostNetwork。
- **场景 (Scenario)**：`hardware_type=Ascend950` 且 `ASCEND_GLOBAL_RESOURCE_CONFIG` 含 `uboe:device` 的 K8s IPv6 单栈部署，同一节点上有多个 Pod。
- **关键词 (Keywords)**：A5, IPv6, hostNetwork, 同机地址重复
