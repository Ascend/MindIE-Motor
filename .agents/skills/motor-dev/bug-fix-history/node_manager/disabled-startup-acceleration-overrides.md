# [2026-09-21] 关闭 vLLM 启动加速仍覆盖原生引擎配置

- **现象 (Symptom)**：`enable_startup_plan=false`、`enable_graph_reuse=false` 时，NodeManager 仍向引擎写入 Motor 默认环境变量和 Ascend 编译配置，覆盖用户的原生 vLLM 配置。
- **根因 (Root cause)**：`motor/node_manager/core/services/native_engine/startup_acceleration.py:build_vllm_startup_environment` 无条件生成缓存目录和 StartPlan 关闭值，`build_vllm_graph_reuse_engine_overrides` 无条件生成图复用关闭值；`service.py:NativeEngineService._pull` 无条件消费这些覆盖。
- **为什么会写出 (Why)**：把功能关闭误建模成“由 Motor 显式配置关闭参数”，而正确的所有权边界应是“Motor 不管理该功能，也不改引擎参数”。
- **修复 (Fix)**：关闭的功能返回空覆盖；仅在至少一个加速功能开启时解析缓存路径和执行预检；开启单项功能时只写该功能必需的参数。
- **测试拦截 (Test interception)**：环境构建单测覆盖全关和单项开启；服务层回归用例预置引擎环境，断言全关时环境原值、引擎配置和预检调用均保持不变。
- **场景 (Scenario)**：用户在引擎配置或容器环境中自定义编译、图模式或缓存路径，同时保持 NodeManager 的 vLLM 启动加速开关关闭。
- **关键词 (Keywords)**：NodeManager, vLLM startup acceleration, disabled feature, engine override, environment passthrough
