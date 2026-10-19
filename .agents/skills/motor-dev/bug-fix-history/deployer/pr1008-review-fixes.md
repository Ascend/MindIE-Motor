# [2026-09-20] 引擎补丁入口被跳过且 CPU 事件跨 TP 分片误报

- **现象 (Symptom)**：分角色启动不执行引擎补丁；CPU 事件将其他 TP worker 尚未完成写入的块报告为已存储，导致亲和命中虚高。
- **根因 (Root cause)**：`examples/deployer/startup/roles/engine.sh` 注释了 `apply_patches`；`vllm_ascend_store_cpu_event_grain.patch` 的事件构造按整个请求枚举块，没有保留 mask 和 TP 分片后的写入范围。
- **为什么会写出 (Why)**：补丁注册测试没有覆盖实际启动入口；粒度转换把请求拥有的 hash 当成当前 worker 已确认存储的块，缺少分片写入行为测试。
- **修复 (Fix)**：恢复启动时执行补丁并在失败时停止；在 missing 过滤前保存当前 worker 的选中范围，lookup 已确认的块和 put 成功的块才生成事件，仅发布完整 grain；保留全局 parent hash。CPU 补丁标记升级为 v2，旧标记不能掩盖补丁应用失败。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_patch_apply_ascend_store_connector.py` 覆盖启动成功/失败、TP 分片全部缺失/部分已存在/全部已存在、put 异常、空范围和部分 grain、旧标记拒绝。执行补丁中的真实发送逻辑，以 fake store 替代 NPU 依赖；新增关键用例在旧代码上失败。
- **场景 (Scenario)**：分角色部署；AscendStore 的 TP 分片异步存储及 CPU KV 事件。已有 v1 补丁的引擎文件应先恢复为对应版本原始源码再应用 v2，不能将旧 marker 视为升级成功。
- **关键词 (Keywords)**：apply_patches, AscendStore, TP sharding, BlockStored, CPU grain
