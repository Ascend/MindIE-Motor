# replay 完整帧与 DP 恢复

- **现象**：Conductor 启动后无法恢复部分引擎或后续 DP 的已有缓存索引。
- **根因**：按三帧读取四帧回复会将 topic 当作 sequence；实例已存在时后续 DP 不触发 replay。
- **为什么会写出**：把一种发布器的帧布局和实例级存在性当成通用恢复契约。
- **修复**：原子接收 multipart 并校验三/四帧；按实例和 DP 判断首次登记。
- **测试拦截**：真实 ROUTER/DEALER 验证两种回复、8 字节 topic、两个 DP 索引恢复与重复登记；分别恢复两处旧逻辑均失败。
- **场景**：Conductor 重启、多 DP 首次登记；恢复仍受引擎保留的历史事件限制。
- **关键词**：replay, multipart, topic, dp_rank, registration。
