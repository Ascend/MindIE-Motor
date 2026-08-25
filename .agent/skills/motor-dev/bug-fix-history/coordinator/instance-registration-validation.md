# [2026-08-24] Instance registration accepted ID collisions and incomplete engine readiness

- **现象 (Symptom)**：独立部署实例可能生成相同的 CRC32 ID；重复 ID 可覆盖其他角色实例，SET 可静默丢失请求项，按 endpoint 删除可能删除错误实例，或在同组 endpoint 顺序变化后无法删除；同一 endpoint 组换序后还会派生不同 job name 和 endpoint ID；按 ID 删除会提交伪造的默认角色和名称；`/v1/models` 返回空列表时显式模型注册仍可通过健康检查；未知 Endpoint 参数会被静默丢弃。
- **根因 (Root cause)**：`register.py` 仅使用角色和首个 endpoint 的 CRC32 低 31 位，且 job name 与 endpoint ID 依赖 CLI 输入顺序；`InstanceManager` 分角色检查 ID，SET 在校验前构造 ID 字典，DEL 只按 ID 删除；初版 DEL 身份签名又包含按输入顺序分配的 endpoint ID，按 ID 删除则用 schema 占位字段直接提交；`probe_endpoint()` 只在模型列表非空时检查；`Endpoint.__init__()` 使用 `**_unused` 接收所有未知参数。
- **为什么会写出 (Why)**：把确定性哈希误当成唯一 ID，且只在单个角色池内考虑重复；把空模型列表当成“无法验证但可继续”；为旧字段保留的宽松参数入口没有限制未知字段范围。
- **修复 (Fix)**：独立部署 ID 使用高位命名空间并覆盖排序后的完整 endpoint 组，job name 与 endpoint ID 使用同一规范顺序；请求、CLI 和 InstanceManager 分层校验重复及冲突，endpoint DEL 按角色及顺序无关的物理 endpoint 集合查询实际实例，按 ID 删除先查询并补全真实身份，Scheduler 拒绝时不更新 Mgmt 镜像；空模型列表判定不健康；删除 Endpoint 的 `**_unused`。
- **测试拦截 (Test interception)**：`test_register.py` 覆盖真实 CRC 碰撞、完整组派生、顺序规范化、空模型列表、按真实身份安全删除及 endpoint 重排；`test_coordinator_instance_manager.py` 覆盖跨角色冲突、SET 请求重复、DEL 身份不匹配和重排后删除；`test_instance.py` 覆盖未知 Endpoint 字段拒绝。
- **场景 (Scenario)**：独立部署实例数量增长、多角色实例共存、管理 API 收到重复 ID、引擎冷启动返回空模型列表，或 Endpoint 参数拼写错误时。
- **关键词 (Keywords)**：Coordinator, CRC32 collision, instance ID, Endpoint extra fields, empty models
