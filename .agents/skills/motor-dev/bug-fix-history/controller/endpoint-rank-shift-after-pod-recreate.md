# [2026-11-19] Pod 故障重建后 DP rank 从 0/1 漂移到 1/2 导致注册失败

- **现象 (Symptom)**：DP=2 实例故障重启后按 DP rank 1、2 拉起，rank 0 缺失，引擎 DP 组校验失败。控制器日志可见
  新 Pod 注册时按当前 endpoint 总数分配 id，装配完成时 endpoint id 集合为 `{1, 2}`。
- **根因 (Root cause)**：`InstanceAssembler._build_endpoints`（`motor/controller/core/instance_assembler.py:501`）
  用当前 endpoint 总数作 `id_offset = metadata.instance.get_endpoints_num()`，而多 endpoint DP 模式下 endpoint id 就是 DP rank。
  故障 Pod（旧 IP）死后其 endpoint 仍留在 `metadata.instance.endpoints`，要等装配线程
  `_assemble_instance → _filter_abnormal_endpoints` 探测 NodeManager 不可达才删除（默认 30 秒一轮）。
  新 Pod（K8s 重建后 IP 变化）在这之前注册，就按残留计数拿到 1、2；随后死 Pod 被过滤，总数回到 2 == dp_size，
  而 `is_endpoints_enough()` 只比较数量、不校验 id 集合，于是 `{1, 2}` 被当作装配完成交给 InstanceManager。
- **为什么会写出 (Why)**：此前修复（commit 2e5445dd「修复同一pod快速重复注册导致endpoint id错误问题」）只加了按
  `pod_ip` 的去重判断，且该判断是锁外的 check-then-act；它假设「重复注册 = 同一 pod_ip」，但 K8s 重建后的 Pod 换了 IP，
  去重失效，而 id 分配仍按计数偏移。遗漏了「endpoint id 必须是 `0..dp_size-1` 的稳定槽位」这一不变量。
- **修复 (Fix)**：`register()` 在单节点多 endpoint 模式（`enable_multi_endpoints && nnodes <= 1`）下从
  `[0, dp_size)` 的空闲 rank 槽位分配 endpoint id（`_allocate_endpoint_ids`）；槽位用满时返回 -1，让 NodeManager
  退避重试，等装配线程过滤死 Pod 释放槽位后再注册。跨节点 PCP（`nnodes > 1`）与单 endpoint 模式保持原计数分配。
  同时把去重判断、rank 分配、`add_node_mgr` / `add_endpoints` 放进同一个 `metadata.lock` 临界区，消除 check-then-act 窗口。
  `Instance` 新增 `get_endpoint_ids()` 读取已占用 rank。
- **测试拦截 (Test interception)**：`tests/controller/core/test_instance_assembler.py` 新增
  `test_register_replacement_pod_reclaims_missing_dp_rank`（旧代码 `assert 0 == -1`：新 Pod 拿到越界 rank）、
  `test_register_fills_lowest_free_dp_rank`（旧代码 `assert [1, 1] == [0, 1]`：新 Pod 与存活 Pod 撞 rank）、
  `test_register_duplicate_pod_keeps_endpoint_ids`、`test_register_cross_node_pcp_keeps_count_based_endpoint_ids`；
  `tests/controller/core/test_instance.py` 覆盖 `get_endpoint_ids()`。
- **场景 (Scenario)**：DP 实例装配期间某个 Pod 故障，K8s 以新 IP 重建 Pod；旧 endpoint 尚未被装配线程过滤时新 Pod 完成注册。
  多 Pod 同时重建时更易触发（两个新 Pod 分别拿到 1、2 或 2、3）。
- **关键词 (Keywords)**：DP rank, endpoint id, register, pod recreate, rank slot, InstanceAssembler
