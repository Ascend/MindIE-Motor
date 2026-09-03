---
name: motor-deploy-k8s
description: Explicit atomic workflow under motor-deploy to deploy, inspect, restart, or stop Motor with native deploy.py, delete.sh, and kubectl.
---

# Motor Kubernetes lifecycle

使用选定执行环境、明确 kube context 和当前 Motor checkout。禁止 workspace wrapper、
本地 run ID 和第二套 deploy engine。

## Deploy

成功完成 config dry-run 后，展示 endpoint/host、context、config directory、namespace
和精确命令，取得明确授权后执行。

**Suite unattended 例外：** 当调用方来自 `motor-smoke-suite` 且传入 suite
pre-authorization（`mode=unattended`、`profile_id`、精确 target、匹配的
`deploy` 或 `delete_owned` token）时，不再重复询问授权；超出 profile 范围则
`BLOCKED`。

`deploy.py` 的 `kubectl apply` 使用 ambient current-context，不接受 context 参数。
若 current-context 与确认的 `CTX` 不一致，资源会写入错误集群，而后续
`kubectl --context "$CTX"` 查询又去目标集群，造成误判。执行前 fail closed：

```bash
if [ "$(kubectl config current-context)" != "$CTX" ]; then
  echo "BLOCKER: current-context $(kubectl config current-context) != CTX $CTX"
  exit 1
fi
```

禁止通过切换共享 endpoint 的 ambient current-context 来“修复”；不一致时停止并与
用户核对，或使用隔离 `KUBECONFIG` 且其 current-context 为 `$CTX`。`--update_config`、
`--update_instance_num` 和 Stop 路径同样适用。

```bash
cd <motor-root>/examples/deployer
python3 deploy.py --config_dir <config-dir>
```

依据退出码和当前集群对象检查结果：

```bash
kubectl --context "$CTX" get all -n "$NS"
kubectl --context "$CTX" get pods -n "$NS" -o wide
kubectl --context "$CTX" get events -n "$NS" --sort-by=.lastTimestamp
```

只等待原生 deployer 本次实际生成的 workload kinds。验证 Pod Ready、Service
Endpoint；涉及代码替换时通过容器内 module `__file__`、包版本和启动日志证明。
原生命令非零退出、workload 有界等待超时或必需 Service/Endpoint 缺失时，先保存
完整 stdout/stderr、时间窗、manifests 和集群现场，再调用
`motor-diagnosis`。这不授权重试或修改。

部署成功只表示命令成功且当前必需 workload Ready；Coordinator readiness 仍必须由
`motor-validation-smoke` 判定。

## Status

Status 只读。查询当前 workloads、Pods、Services、Endpoints、Events 和相关日志，
不得从历史报告推断当前成功。

## Restart / component rollout

展示精确目标并取得授权。发现实际 Deployment/StatefulSet 后只重启指定对象，禁止
`--all`。

- 单组件模板、配置或 wheel 调试：只更新该组件需要的 ConfigMap/YAML，并只 rollout
  对应 Controller/Coordinator；P/D 保持运行。
- 单个在线 ConfigMap JSON 字段：读取当前 live value，生成最小 patch，检查消费该
  ConfigMap 的具体 workload，再执行定向 rollout。
- 共享 `boot.sh` wheel 替换：由 `motor-deploy-build-wheel` 完成可见性 gate 和标记块修改，
  本 Skill 只负责经授权更新配置并定向 rollout。

更新后检查新 Pod UID、rollout 状态、配置实际生效值和目标组件日志。Controller 与
Coordinator 的健康不能互相替代。

## Stop

展示 namespace 和待删对象并取得授权。`delete.sh <NS>` 对 `output_yamls/*.yaml` 执行
`kubectl delete -f` 时不绑定 context、也不传 `-n "$NS"`，且会 force-delete 卡住的
Terminating Pod 并删除固定的 `motor-config` ConfigMap。使用前必须全部满足，否则只删
本次生成的精确 manifest：

1. 执行 Deploy 中的 fail-closed context 校验（`delete.sh` 自身无 context 绑定）。
2. 证明 `output_yamls/*.yaml` 均属于本次部署：

    ```bash
    grep -L "namespace: $NS" output_yamls/*.yaml   # 必须无输出
    grep -h "namespace:" output_yamls/*.yaml | sort -u   # 必须只有 $NS
    ```

3. 确认 `$NS` 专用于本次 Motor 部署（无无关 workload 共享）。

任一无法证明时，禁止运行 `delete.sh`；改用
`kubectl --context "$CTX" delete -f <file> -n "$NS"` 只删本次 manifest。禁止删除
namespace 或无关资源。

所有操作报告实际命令和操作后的当前状态。
