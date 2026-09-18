# [2026-09-16] YuanRong 节点 pool replay 丢失无 backend_id 的事件

- **现象 (Symptom)**：节点 pool 的 replay 清理事件未携带 `backend_id` 时，节点上各 DP 的 CPU 缓存记录未被清除。
- **根因 (Root cause)**：实时订阅使用 endpoint 中的节点 IP，replay 却使用 pool 的注册 instance_id，无法匹配 HBM IP 索引；此外 replay 优先尝试 vLLM 解析，未带 `type` 的 pool map 会被接受为未知引擎事件并忽略。
- **为什么会写出 (Why)**：只共享了匹配策略，未共享完整路由身份和事件来源；仅检查注册请求无法验证 replay 实际入库行为。
- **修复 (Fix)**：`registry.rs` 在创建订阅前统一解析 backend_id、MatchMode 和 IP 索引，并将同一结果交给 replay。`zmq_subscriber.rs` 的 pool replay 复用实时 pool 格式分流。
- **测试拦截 (Test interception)**：`registry::tests::test_node_pool_replay_without_backend_id_clears_node_dps` 使用真实 ZMQ ROUTER 回放无 backend_id 的 CPU cleared 消息，验证两个 DP 的 CPU 缓存均被清理；修复前失败、修复后通过。
- **场景 (Scenario)**：YuanRong CPU/Disk 节点 pool 配置 replay_endpoint，回放事件省略 backend_id。
- **关键词 (Keywords)**：YuanRong, replay, backend_id, IpOnly, EventSource。
