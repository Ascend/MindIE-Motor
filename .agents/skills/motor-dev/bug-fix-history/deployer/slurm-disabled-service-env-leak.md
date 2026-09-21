# [2026-09-20] Slurm 禁用的可选服务仍向容器泄漏占位环境变量

- **现象 (Symptom)**：未配置 KV Conductor 时，Slurm 提交环境仍包含 `KV_CONDUCTOR_SERVICE=<kv-conductor-ip>`；若被 Apptainer 继承，Motor 会将非空地址视为启用 KV Conductor，并尝试注册实例或提前加载 Tokenizer。
- **根因 (Root cause)**：`examples/deployer/slurm_deploy.py::_export_runtime_env()` 无条件导出可选服务地址，`examples/deployer/slurm_job.sh` 的 `apptainer exec` 又未使用 `--cleanenv`，使按角色构造的显式环境变量列表不能阻止宿主作业环境进入容器。
- **为什么会写出 (Why)**：只检查了 `KV_RUNTIME_ENV` 的条件注入，没有同时考虑 `sbatch --export=ALL` 与 Apptainer 默认环境继承形成的旁路。
- **修复 (Fix)**：禁用对应服务时将 `KVS_MASTER_SERVICE`、`KV_CONDUCTOR_SERVICE`、`MF_STORE_SERVICE` 和可能残留的 `ASCEND_MF_STORE_URL` 置空；为 `apptainer exec` 增加 `--cleanenv`，容器只接收按角色显式传递的运行变量。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_slurm_deploy.py` 验证禁用服务时占位值和旧 MF Store URL 被清空、启用时真实地址保持不变，并断言容器执行使用 `--cleanenv`。
- **场景 (Scenario)**：使用 Slurm 部署且未启用 KV Conductor、KV Store 或 MF Store，同时 CLI 默认值或提交宿主环境中存在对应地址变量。
- **关键词 (Keywords)**：Slurm, Apptainer, cleanenv, KV_CONDUCTOR_SERVICE, placeholder
