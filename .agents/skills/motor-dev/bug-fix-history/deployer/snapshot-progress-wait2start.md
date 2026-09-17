# [2026-09-17] 容器快照部署进度条停在 90%

- **现象 (Symptom)**：启用容器快照后，Endpoint 最终已恢复为 `NORMAL`，但 `deploy.py` 启动进度条仍停留在 `Graph capturing finished` 对应的 90%。
- **根因 (Root cause)**：`examples/deployer/lib/tui/step.py` 的完成标记只识别 `EndpointStatus.INITIAL to EndpointStatus.NORMAL`；快照健康检查先返回 202 时，状态会经过 `WAIT2START`，最终日志为 `EndpointStatus.WAIT2START to EndpointStatus.NORMAL`，未被识别为 100%。
- **为什么会写出 (Why)**：进度解析假设 Endpoint 只会从 `INITIAL` 直接进入 `NORMAL`，遗漏了快照健康协议引入的中间状态。
- **修复 (Fix)**：保留普通启动完成标记，并新增 `EndpointStatus.WAIT2START to EndpointStatus.NORMAL` 的 100% 映射。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_tui_step.py::test_normal_endpoint_transition_completes_startup_progress` 参数化验证普通启动和快照启动的最终状态迁移都返回 100%。
- **场景 (Scenario)**：启用 `motor_container_snapshot_config.enable_snapshot`，且状态轮询在 checkpoint done 前观察到 `/snapshot/health` 返回 202。
- **关键词 (Keywords)**：deployer、container snapshot、progress、WAIT2START、90%
