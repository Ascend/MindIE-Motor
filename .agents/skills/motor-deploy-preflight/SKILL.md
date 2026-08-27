---
name: motor-deploy-preflight
description: Explicit atomic workflow under motor-deploy for read-only Kubernetes and MindCluster checks. Use for 部署前检查、环境检查或 Motor deploy feasibility after deployment routing.
---

# Motor deploy preflight framework

只读检查目标 kube context 的当前事实，不创建资源、不改配置，不使用未被当前 Motor
源码/文档证实的固定 CRD 或组件名称。

1. 从当前原生 config 确认 deploy mode、namespace、镜像、NodePort、NPU/拓扑和存储。
2. 检查 API 可达与 RBAC：version、`auth can-i`、api-resources。
3. 从当前 deployer/docs 确认该 mode 需要的 CRD/operator/scheduler，再检查对象存在和
   Ready；不能确认的项标记待确认，禁止猜名称。
4. 检查可调度节点、NPU resource/容量、taint/selector/affinity、组件状态、storage 和
   网络前提。未逐卡核验容量时不能标 PASS。
5. 检查端口合法、本批唯一且未被当前 Service 占用；冲突只报告占用者和候选值，
   不自动写回 config。
6. 镜像只检查引用格式、registry/pull prerequisites 和已有 workload 证据；本版本不
   创建 DaemonSet 扫描节点本地镜像，不能声称全节点覆盖。

API/RBAC 不可用或已证实硬依赖缺失时 fail closed。输出检查项、命令/对象证据、
PASS/FAIL/待确认和最小下一步。不写 run gate，不声称“环境通过必然能部署”。
