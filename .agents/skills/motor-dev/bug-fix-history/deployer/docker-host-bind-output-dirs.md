# [2026-11-19] Docker 创建前预检误拒可自动创建的目录和兼容驱动路径

- **现象 (Symptom)**：Docker 部署在创建容器前报 `Host bind path(s) do not exist`。`/root/ascend/log`、`/root/.cache`、`/var/coredump`、`/var/log` 等输出目录未准备时无法启动；KV Store 模板中的 `/driver` 不存在时也会失败，即使 `/usr/local/Ascend/driver` 已存在。
- **根因 (Root cause)**：`examples/deployer/docker_deploy.py` 的 `_require_host_binds()` 将所有宿主机 bind 源路径统一按“必须已经存在”处理，没有区分可创建的输出目录和必须预置的输入路径。`examples/deployer/lib/constant.py` 的 KV Store 模板同时挂载标准驱动目录和 `/driver` 兼容目录，未提供后者缺失时的回退。
- **为什么会写出 (Why)**：把 Kubernetes `hostPath` 的目录准备语义直接搬到 Docker 预检，并假设所有节点的 Ascend 驱动都提供 `/driver` 兼容目录，遗漏了不同节点驱动安装布局的差异。
- **修复 (Fix)**：对四个明确的输出目录在白名单内执行 `os.makedirs(..., exist_ok=True)`，创建失败时给出具体路径；`/data` 仍按输入路径严格预检。创建 KV Store 容器前，若 `/driver` 不存在而标准驱动目录存在，则只移除 `/driver` 兼容挂载；两者都不可用时继续报错。普通 P/D 模板不修改。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_docker_utils.py` 新增 5 个模块级用例，覆盖输出目录创建、`/data` 不自动创建、创建失败、驱动回退、保留已有兼容挂载和双路径缺失。旧代码上 5 个新增用例全部失败；修复后包含相邻 bind 用例的定向测试 7 个全部通过。
- **场景 (Scenario)**：Docker `--create` 或一键部署中，节点未提前创建日志、缓存或 coredump 目录，或 KV Store 节点只有 `/usr/local/Ascend/driver` 而没有 `/driver`。驱动回退只判断目录是否存在，不校验驱动库完整性，也不替代真实 KV Store 启动验证。
- **关键词 (Keywords)**：deployer、Docker bind、hostPath、/driver、KV Store
