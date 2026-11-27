# [2026-11-21] Ascend 950 Docker 容器正确挂卡但 ACL 找不到设备

- **现象 (Symptom)**：仅映射非零物理卡的 Ascend 950 Docker 容器，用同号的 `ASCEND_RT_VISIBLE_DEVICES` 启动时返回 `aclInit 107001`，可用卡数为零。改用容器逻辑索引后，同一个容器能初始化并执行 NPU 运算。
- **根因 (Root cause)**：`examples/deployer/startup/common.sh` 和原生引擎服务把物理卡号直接传给 CANN 的逻辑设备过滤器。
- **为什么会写出 (Why)**：宿主机和全卡可见容器经常使用相同编号。只映射部分卡时，设备节点保留物理号，CANN 从零开始枚举容器可见设备。
- **修复 (Fix)**：Docker 部署器记录 Ascend 950 的物理映射。NodeManager 继续使用物理号上报设备归属，启动引擎时把所选设备换算成容器逻辑索引。
- **测试拦截 (Test interception)**：Docker 测试覆盖高编号卡、A5 Pod16、手工 `--start --devices` 与缺失卡。引擎测试断言每个 endpoint 同时收到正确的物理卡号和逻辑索引。
- **场景 (Scenario)**：Ascend 950 Docker 引擎需要隔离物理卡，尤其是高编号卡或两个实例使用不同卡组时。
- **关键词 (Keywords)**：Ascend 950、Docker、ACL 107001、ASCEND_RT_VISIBLE_DEVICES、物理卡

## 2026-11-22 补充：额外挂卡和同容器偏移的归属

- **现象 (Symptom)**：挂载物理卡 `2,3,4,5,6,7`、配置 4 张计算卡时，worker 使用前四张，登记省略了物理号。同容器挂载 `2,4,6,7`、角色偏移 2 时，worker 使用逻辑卡 `2,3`，登记把这两个索引作为物理号上报。
- **根因 (Root cause)**：`register_manager.py` 将完整挂载列表与角色计算卡数直接比较。合并容器路径在读取 A5 设备映射前就用偏移生成卡号。
- **为什么会写出 (Why)**：挂载列表、角色的计算卡列表和 CANN 的本地索引承担不同作用。用于通信的额外挂卡和非连续物理号会让三者产生差异。
- **修复 (Fix)**：登记和启动共享 A5 角色选卡，优先使用显式卡号顺序，其余按挂载列表和配置偏移选择角色的计算卡。登记使用物理号，worker 使用对应的容器索引。
- **测试拦截 (Test interception)**：NodeManager 登记和启动测试覆盖额外挂卡、显式顺序、同容器偏移和越界。还检查单 endpoint 的 worker 是否收到完整角色分配。
- **场景 (Scenario)**：Ascend 950 Docker 部署为通信额外挂卡，或多个角色在一个容器内分别使用非连续物理卡。
- **关键词 (Keywords)**：Ascend 950、Docker、physical_device_ids、device_offset、额外挂卡
