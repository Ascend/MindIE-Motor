# [2026-09-08] Slurm 各容器生成不同 service_id

- **现象 (Symptom)**：Slurm 的每个 task 都在容器内执行 `set_env_docker.py`；镜像缺少 `tzdata` 时初始化失败，即使依赖齐全，各 task 也可能生成不同的 `service_id`。
- **根因 (Root cause)**：`examples/deployer/slurm_deployer/script/run_motor.sh` 在每个 Apptainer 实例内调用 `prepare.sh`，将本应属于部署级别的配置生成放到了 task 级别。
- **为什么会写出 (Why)**：将 configmap 当作容器本地临时目录，没有保留 `service_id` 是整次部署共享标识这一约束，也没有与 K8s 的宿主机预处理边界对齐。
- **修复 (Fix)**：以平级入口 `examples/deployer/slurm_deploy.py` 取代独立 Bash 目录；它直接使用公共方式读取和校验 `user_config`，并复用 K8s multi-deployment 的组件启用判断和 KV 配置归一化，不建立第二套配置模型，也不修改 `set_env_docker.py` 的共享接口。`slurm_deploy.py` 的 `_prepare_configmap()` 直接准备仓库内静态启动文件，并且只复制命令行解析得到的 `user_config.json` 和 `env.json`，不扫描配置文件的父目录；公共环境由 `set_env_docker.py` 渲染，MF Store 环境在同一 Python 准备流程中补充。KV Store 与 KV Conductor 独立启用，Conductor 可连接本次启动或已有的 KV Store，不会隐式分配 KV Store 节点。`COORDINATOR_INFER_SERVICE` 和 `COORDINATOR_OBS_SERVICE` 直接继承 `COORDINATOR_SERVICE`。提交作业前由宿主机生成一次 `./slurm_workspace/configmap`，所有 task 将 `./slurm_workspace` 只读挂载到容器内的 `/motor/slurm_workspace`，容器不再执行 prepare。`stop` 只取消作业并完整保留工作区现场；下一次 `start` 才重建 ConfigMap 和 Job ID 文件，历史日志持续保留。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_slurm_deploy.py::test_prepare_runs_once_on_host_and_copies_resolved_config` 验证宿主机只复制解析后的两个配置文件，并生成统一 `service_id`、渲染 MF Store 环境及准备 Memcache 默认配置；池化测试验证 Memcache KV Store 与 KV Conductor 均会提交，同时验证单独启用 Conductor 不会分配 KV Store 节点；CLI 测试确保只暴露一个 Coordinator 地址；启动测试确保 infer/obs 地址自动复用 Coordinator 地址；挂载测试断言容器不再执行 prepare；停止测试确保只清理 `slurm_workspace`。
- **场景 (Scenario)**：使用多个 Slurm task/角色部署 Motor，尤其是运行时镜像未安装 Python `tzdata` 时。
- **关键词 (Keywords)**：Slurm、service_id、set_env_docker、tzdata、Apptainer
