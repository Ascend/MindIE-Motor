---
name: motor-deploy-build-wheel
description: Explicit atomic workflow under motor-deploy to build a target-compatible Motor wheel, make it visible to Pods, update remote boot.sh, and orchestrate an authorized targeted rollout.
---

# Motor wheel build and remote replacement

构建完整 Motor wheel（protobuf + Rust kv-conductor + Python wheel），供部署 Pod 的
`examples/deployer/startup/boot.sh` 安装。源码开发不需要本 Skill；部署代码替换禁止用
源码树 `PYTHONPATH`。

## 必填事实

- `REMOTE_MOTOR_ROOT`：目标环境使用的 Motor checkout；
- `SOURCE_SHA`：该源码的完整 commit SHA；dirty tree 还需记录 diff/content digest；
- `BASE_IMAGE_REF`：与目标 Pod 运行时兼容的构建镜像，禁止猜测；
- `WHEEL_OUTPUT_DIR`：远端构建产物目录；
- `CTX`、`NS` 和需要更新的明确 workloads；
- 远端执行方式及共享存储边界。

构建、修改远端 `boot.sh`、调整挂载、更新配置和 rollout 都是写操作。执行前展示具体
目标、命令、影响范围和回退方式并取得明确授权。

## 1. 在目标兼容容器内构建

使用一个受监控的长任务，只启动一次：

```bash
mkdir -p "$WHEEL_OUTPUT_DIR/dist"
docker run --rm --network=host \
  -v "$REMOTE_MOTOR_ROOT:/src/motor:ro" \
  -v "$WHEEL_OUTPUT_DIR/dist:/out" \
  -w /work "$BASE_IMAGE_REF" \
  bash -c 'set -euo pipefail; cp -r /src/motor /work/motor; cd /work/motor; bash build.sh; cp dist/motor-*.whl /out/'
```

Skill 不自动 pull 镜像。同步命令超时后不得启动重复构建。构建目录必须按 source
identity 隔离；只有用户明确要求 reuse 且 wheel/hash/source identity 完全匹配时复用。

## 2. 产物 gate

```bash
cd "$WHEEL_OUTPUT_DIR/dist"
set -- motor-*.whl
test "$#" -eq 1 && test -f "$1"

# kv-conductor 完整性 gate：上游 build.sh 在无 cargo 且无预编译 bin 时只 warning 仍产出 wheel。
if ! unzip -l "$1" | grep -q 'motor/kv_conductor/bin/kv-conductor'; then
  echo "BLOCKER: wheel 缺少 motor/kv_conductor/bin/kv-conductor（build.sh 跳过了 Rust 构建）"
  exit 1
fi
unzip -p "$1" 'motor/kv_conductor/bin/kv-conductor' > /tmp/kv-conductor.check
test -s /tmp/kv-conductor.check || { echo "BLOCKER: 解出的 kv-conductor 为空"; exit 1; }
chmod +x /tmp/kv-conductor.check && test -x /tmp/kv-conductor.check
rm -f /tmp/kv-conductor.check

sha256sum "$1" > "$WHEEL_OUTPUT_DIR/wheel.sha256"
sha256sum -c "$WHEEL_OUTPUT_DIR/wheel.sha256"
```

必须恰好一个 wheel，且 wheel archive 内包含非空可执行的
`motor/kv_conductor/bin/kv-conductor`；复用已有 wheel 时也必须通过本 gate。报告
`SOURCE_SHA`、dirty/content identity、base image、wheel 绝对路径和 sha256。任何歧义都停止。

## 3. 共享盘与逐节点可见性 gate

先确认 wheel 所在宿主机文件系统：

```bash
df -T "$WHEEL_OUTPUT_DIR"
findmnt -T "$WHEEL_OUTPUT_DIR"
```

- NFS/Lustre/GPFS/Ceph/GlusterFS 等共享文件系统：证明所有目标节点挂载同一绝对路径。
- ext4/xfs 等本地盘：不能假设其他节点可见。经明确授权后，将 wheel 和 sha256 复制
  到每个目标节点的同一绝对路径，并逐节点校验 hash；或改用共享路径。

接着读取**目标环境实际 workload YAML**，确认该绝对路径位于目标容器已挂载 volume
的 `mountPath` 下，并在已有 Pod 中验证：

```bash
kubectl --context "$CTX" -n "$NS" get <kind> <workload> -o yaml
kubectl --context "$CTX" -n "$NS" exec <pod> -- ls -l "$WHEEL_OUTPUT_DIR/dist"
```

未挂载时，先提出最小变更：engine Pod 优先用原生 `motor_deploy_config.storage`；
Controller/Coordinator 按实际模板/生成 YAML 增加挂载。配置或模板修改须单独授权。
共享存储、逐节点复制和 Pod mount 三者任一未证明时，禁止修改 `boot.sh` 和 rollout。

## 4. 精确修改远端 boot.sh

重新读取 `$REMOTE_MOTOR_ROOT/examples/deployer/startup/boot.sh`，并同时核对当前
`setup.py` 的 distribution name。当前 upstream `boot.sh` 是 role dispatcher，没有内置
wheel 安装块；在 `source "$SCRIPT_DIR/common.sh"` 之后、`case "$ROLE" in` 之前插入
一个自包含标记块，使所有角色在分派前完成同一 wheel 安装：

```bash
# >>> MOTOR_AGENT_WHEEL_DIR_BEGIN
MOTOR_WHEEL_DIR="<WHEEL_OUTPUT_DIR>/dist"
shopt -s nullglob
motor_agent_wheels=("${MOTOR_WHEEL_DIR}"/motor-*.whl)
if [ "${#motor_agent_wheels[@]}" -ne 1 ]; then
    echo "ERROR: expected exactly one motor wheel under ${MOTOR_WHEEL_DIR}"
    exit 1
fi
python3 -m pip install --force-reinstall --no-deps "${motor_agent_wheels[0]}" || exit 1
unset motor_agent_wheels
shopt -u nullglob
# <<< MOTOR_AGENT_WHEEL_DIR_END
```

- 已有标记块时只替换 BEGIN/END 之间内容；没有时插入到原生
  `source "$SCRIPT_DIR/common.sh"` 与 `case "$ROLE" in` 之间。
- 写后重读，BEGIN/END 必须各一次且路径精确匹配。
- 找不到这两个相邻结构锚点、标记缺失/重复/不成对时停止，不猜插入位置。若后续
  revision 恢复原生 wheel 安装能力，优先复用原生机制并停止维护此标记块。
- 回镜像模式时精确删除完整标记块并重读确认；不得覆盖其他人的 `boot.sh` 修改。

## 5. 更新配置并定向 rollout

展示将更新的 ConfigMap/YAML 和精确 workloads，取得 rollout 授权。使用当前原生
deployer 的配置更新能力或仅 apply 涉及 `boot.sh`/目标组件的 manifests，然后只对
目标 Controller/Coordinator/P/D workload 做定向 rollout；禁止 `--all`。

若只替换 Controller/Coordinator，不能顺手重启 P/D。若共享 `boot.sh` 已作用于所有
角色，必须明确说明后续 P/D 重启也会安装该 wheel。

验收：

1. rollout 完成且新 Pod UID 可见；
2. 启动日志证明卸载旧 Motor 并安装目标 wheel；
3. 容器内包版本、module `__file__` 和 wheel hash/source identity 一致；
4. `motor-validation-smoke` readiness 通过；需要时再跑 `motor-validation-functional`。

失败时保留构建日志、boot.sh diff、manifests、rollout 和 Pod 日志，进入
`motor-diagnosis`；不得自动重试或扩大重启范围。
