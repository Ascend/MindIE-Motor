# [2026-09-30] Kubernetes 主备倒换时 Coordinator Daemon 退出导致 Pod 重启

- **现象 (Symptom)**：运行中的主 Coordinator 丢失 etcd 锁后，在 Kubernetes 环境调用 `os._exit(1)`，容器退出并被 kubelet 按重启策略重拉。
- **根因 (Root cause)**：`motor/coordinator/daemon/coordinator_daemon.py` 的 `_on_become_standby` 被主备快恢改动为 Kubernetes 专用退出分支。
- **为什么会写出 (Why)**：将“尽快从推理 Service 摘除旧主”的需求绑定为退出进程，忽略了主备切换期间保留 Pod 与 Mgmt/Obs 的运行语义。
- **修复 (Fix)**：恢复该函数的原有行为：写入 `ROLE_SHM_STANDBY`，仅停止 InferenceWorkers，保留 Mgmt 与 Obs；不再按 Kubernetes 环境调用 `os._exit(1)`。
- **测试拦截 (Test interception)**：`tests/coordinator/test_main.py::test_on_become_standby_does_not_exit_on_kubernetes` 在 Kubernetes 环境变量存在时断言不会退出，且仅停止 InferenceWorkers。
- **场景 (Scenario)**：启用 Coordinator 主备后，当前主 Coordinator 失去 etcd 锁并切换为 standby。
- **关键词 (Keywords)**：Coordinator, Kubernetes, standby, os._exit, failover
