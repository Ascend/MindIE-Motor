# [2026-09-28] 快照 YAML 误删共享内存挂载

- **现象 (Symptom)**：启用 container_snapshot_config 后，生成的 InferServiceSet 不包含 dshm volume 和 /dev/shm 挂载，配置的共享内存大小无法生效。
- **根因 (Root cause)**：`examples/deployer/lib/container_snapshot.py` 的 `_SNAPSHOT_EXCLUDED_VOLUME_NAMES` 包含 dshm，快照差异渲染删除了已经配置好 sizeLimit 的卷。
- **为什么会写出 (Why)**：将推理需要的内存卷和需要排除的宿主机路径卷一并删除，原测试也错误要求 dshm 不存在。
- **修复 (Fix)**：仅从排除名单移除 dshm，保留模板 emptyDir 和 volumeMount，复用 motor_deploy_config.dshm_size；未配置时保留模板默认值。
- **测试拦截 (Test interception)**：`test_common_template_renders_snapshot_deltas_and_a3_device` 覆盖 PD/混部、默认 4Gi/自定义 128Gi；旧代码因缺少 dshm 失败，修复后快照文件 11 项测试通过。只有启用的角色应用自定义大小，零副本角色保留模板值。
- **场景 (Scenario)**：K8s + CRD 快照部署；这是 YAML 渲染验证，不代表真实 CRIU 恢复已验证。
- **关键词 (Keywords)**：snapshot, dshm, emptyDir, InferServiceSet
