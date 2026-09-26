# [2026-09-25] A5 IPv6 host-nic 部署无法注册且 memcache 入池容量为 0

- **现象 (Symptom)**：A5 UBOE（`uboe:device`）IPv6 单栈上，hostNetwork 引擎访问 Controller ClusterIP 失败；`/dev/ummu`、`/dev/uburma` 按 CharDevice 挂载被 kubelet 拒绝；memcache 默认 `device_sdma` 且 `dram.size=0GB`，`init bm` 为 `hbm{0}, dram{0}`，Alloc 为 0。
- **根因 (Root cause)**：host-nic overlay 未让 Controller 进入 hostNetwork，也未把 Motor Endpoint IP 写入 `/etc/hosts`。A5 设备节点是目录不是单个字符设备。inprocess 模板的 DRAM 配额为 0，协议默认 SDMA，A5 无法完成 device 拷贝。KV store 探针打到 `/livez`（404），进程只提供 `/health`。
- **为什么会写出 (Why)**：把 A3 的 SDMA/hixlep/字符设备假设套到 A5 UBOE；把 ClusterIP DNS 当成 hostNetwork 也能用。
- **修复 (Fix)**：仅在 overlay 打开时为 A5 Controller/引擎启用 hostNetwork、去掉 hixlep、按 Directory 挂载 URMA 设备，并用 Endpoint IP 生成 `service-hosts`。A5 启动时把协议改为 `device_urma`，inprocess `dram.size` 默认 16GB。探针改为 `/health`。未设置 overlay 的 IPv4 Pod 网络路径不变。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_a5_engine_pod_config.py` 覆盖 overlay 才挂 hostNetwork、去掉 hixlep、URMA 目录类型，以及非 A5 不启用。
- **场景 (Scenario)**：`hardware_type=Ascend950` 且 `ASCEND_GLOBAL_RESOURCE_CONFIG` 含 `uboe:device` / `ub_ctp:device` / `roce:device` / `ub_rtp:device` 的 K8s 部署。
- **关键词 (Keywords)**：A5, UBOE, hostNetwork, device_urma, service-hosts
