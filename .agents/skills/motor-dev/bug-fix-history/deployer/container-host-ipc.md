# [2026-10-08] 模型所需宿主 IPC 未传入 Docker/Kubernetes

- **现象 (Symptom)**：Docker 引擎模板仅配置 `--shm-size`，Kubernetes 模板使用独立的 `dshm` emptyDir，无法表达 DeepSeek-V4.1-Flash A3 典配的宿主 IPC 要求。
- **根因 (Root cause)**：容器创建阶段没有读取模型环境配置中的 IPC 开关；仅向容器内导出环境变量无法改变容器命名空间。
- **为什么会写出 (Why)**：容器生命周期参数与引擎进程环境变量需要在不同阶段消费。当前 Slurm/Apptainer 启动方式默认已共享宿主 IPC，不需要适配同一开关。
- **修复 (Fix)**：通过 `lib/container_ipc.py` 解析 `motor_common_env.MOTOR_ENABLE_IPC_HOST`。Docker 启用时用 `--ipc=host` 替换 `--shm-size`；Kubernetes 的部署、dry-run 和扩容入口读取开关，为三种部署模式的引擎 Pod 设置 `hostIPC: true`，移除 `dshm` 卷及 `/dev/shm` 显式挂载，由容器运行时提供宿主共享内存，不添加 hostPath。关闭时保留模板默认行为。Slurm 启动脚本保持不变。
- **测试拦截 (Test interception)**：`test_docker_utils.py` 验证配置读取、命令参数、缺省和非法值，保留轻量导入契约；`test_pd_hybrid.py` 验证 K8s 三种模式的 PD/混部配置、开关开启/关闭/省略、连续生成状态重置、控制面不受影响及扩容生成的 Pod 配置；`test_storage.py` 验证普通容器和初始化容器的共享内存挂载被移除，其他卷保持不变。
- **场景 (Scenario)**：Docker 或 Kubernetes 引擎在同一宿主机上需要共享 IPC；仅验证参数和 YAML 生成，模型实机运行另行验收。
- **关键词 (Keywords)**：Docker, Kubernetes, host IPC, DeepSeek-V4.1-Flash, env.json
