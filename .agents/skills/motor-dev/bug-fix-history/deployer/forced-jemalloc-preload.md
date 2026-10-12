# [2026-10-12] 启动脚本强制预加载 jemalloc 导致自定义算子不兼容

- **现象 (Symptom)**：Motor 启动 vLLM Ascend 时强制设置 jemalloc，和当前 CANN 自定义算子的 tiling 内存管理不兼容。
- **根因 (Root cause)**：四个角色启动脚本调用 `setup_jemalloc`，公共函数通过 `LD_PRELOAD` 把 jemalloc 注入所有后续子进程。
- **为什么会写出 (Why)**：将可选的 CPU 内存分配优化固化为全局启动行为，未保留引擎和运行环境选择分配器的边界。
- **修复 (Fix)**：删除四处调用和公共函数，不再由 Motor 启动脚本修改 `LD_PRELOAD`。
- **测试拦截 (Test interception)**：`test_startup_scripts_do_not_force_jemalloc` 检查启动脚本不再包含 jemalloc 注入，并对受影响 Shell 脚本执行语法检查。
- **场景 (Scenario)**：使用对 jemalloc 不兼容的 CANN/vLLM Ascend 自定义算子启动 Engine 或单容器部署。
- **关键词 (Keywords)**：deployer, jemalloc, LD_PRELOAD, CANN, tiling
