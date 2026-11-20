# [2026-11-20] 重调度后 DP rank 正确但 master_dp_ip 指向 rank 1 的 Pod

- **现象 (Symptom)**：P 实例两个 Pod 重调度后端点 rank 分别为 0、1（正确），但 StartCmd 下发的 `master_dp_ip` 指向
  rank 1 的 Pod；开启快照时 `node_rank == 0` 也会落到 rank 1 的 Pod（错误标记快照 master）。A2/A3 场景下实例级
  ranktable 还残留被过滤 Pod 的 `ServerInfo`（旧 IP），`server_count` 只增不减。
- **根因 (Root cause)**：`InstanceAssembler._send_start_command` 用 `node_managers[0].pod_ip` 作为 `master_dp_ip`，
  并用 `enumerate(node_managers)` 下标作为 `node_rank`，隐含假设「注册顺序 == DP rank 顺序」。故障 Pod 会多注册一次
  （占 rank 0），该 Pod 被 `_filter_abnormal_endpoints` 删除后，rank 0 由后注册的替换 Pod 复用；若健康槽位替换 Pod
  在过滤前注册，它先拿到 rank 1，最终 `node_managers` 顺序为 `[rank1, rank0]`，master 就选错了。另外
  `_filter_abnormal_endpoints` 只删 node_mgr/endpoints，未清理 `metadata.ranktable` 中该 Pod 的 server 条目。
- **为什么会写出 (Why)**：`master_dp_ip`/`node_rank` 是按「列表顺序」推导出来的，而不是按 DP rank 这一权威坐标；
  在计数式分配时代两者恰好一致，rank 改为从 `[0, dp_size)` 空槽分配后该隐式不变量失效。ranktable 聚合只有
  append、没有对应的 remove，和端点/节点的增删不成对。
- **修复 (Fix)**：`master_dp_ip` 改为取非 headless 端点中 id 最小的 Pod（无可视端点时回退首个 node manager 并告警）；
  `nnodes == 1` 时 `node_rank` 取该 Pod 的最小 endpoint id（拥有 rank 0 的 Pod 才为 0），`nnodes > 1` 的 PCP 语义
  不变；`_filter_abnormal_endpoints` 改收 `metadata`，过滤死 Pod 时同步移除 ranktable 中匹配 `server_id`/`container_ip`
  的条目并更新 `server_count`。
- **测试拦截 (Test interception)**：`test_send_start_command_master_and_node_rank_follow_dp_rank`（修复前
  `master_dp_ip` 指向 rank 1、`node_rank` 与 rank 相反）、`test_filter_removes_dead_pod_from_ranktable`（修复前
  ranktable 仍为 `[dead_ip, peer_ip]`、`server_count=2`）。两者在旧代码上按预期失败。
- **场景 (Scenario)**：实例重调度 / Pod 故障重建，期间存在旧 IP 的额外注册并被过滤；注册顺序与最终 DP rank 顺序
  不一致；A2/A3 带 ranktable 部署。
- **关键词 (Keywords)**：DP master, master_dp_ip, node_rank, ranktable, reschedule, InstanceAssembler
