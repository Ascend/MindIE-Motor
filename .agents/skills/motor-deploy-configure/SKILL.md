---
name: motor-deploy-configure
description: Explicit atomic workflow under motor-deploy that validates native user_config.json and env.json with deploy.py dry-run and inspects generated Kubernetes YAML.
---

# Motor deploy configure framework

原生 `user_config.json` 和 `env.json` 是唯一部署配置，不生成第二套 profile、plan、bundle
或 digest gate。

1. 重读配置，报告 job/namespace、mode、image、model/mount、P/D/parallelism、NPU、
   selector、ports 和 package mode；缺失值不猜。
2. 确认引用路径在目标环境可见，实际模板会挂载它们。
3. 在 dry-run **前**对 `output_yamls/` 做快照，dry-run 后只消费本次新增或重写的
   manifest。`deploy.py --dry-run` 只重置进程内 `g_generate_yaml_list`，不清理固定
   `output_yamls/` 目录，旧 mode/config 残留会被误当作本次产物。

   ```bash
   cd <motor-root>/examples/deployer
   SNAPSHOT_BEFORE=$(mktemp)
   find output_yamls -maxdepth 1 -type f \( -name '*.yaml' -o -name '*.yml' \) \
     -exec sha256sum {} + 2>/dev/null | sort > "$SNAPSHOT_BEFORE"

   python3 deploy.py --config_dir <config-dir> --dry-run

   SNAPSHOT_AFTER=$(mktemp)
   find output_yamls -maxdepth 1 -type f \( -name '*.yaml' -o -name '*.yml' \) \
     -exec sha256sum {} + 2>/dev/null | sort > "$SNAPSHOT_AFTER"

   mapfile -t NEW_YAMLS < <(
     join -v1 -j1 <(awk '{print $1, $2}' "$SNAPSHOT_AFTER" | sort) \
       <(awk '{print $1, $2}' "$SNAPSHOT_BEFORE" | sort) | awk '{print $2}'
   )
   if [ "${#NEW_YAMLS[@]}" -eq 0 ]; then
     echo "BLOCKER: dry-run 未产生新的 YAML"
     exit 1
   fi
   rm -f "$SNAPSHOT_BEFORE" "$SNAPSHOT_AFTER"
   ```

4. 非零退出时保存完整命令、stdout/stderr、config 和已生成文件，进入
   `motor-diagnosis`，不自动编辑或重跑。
5. 只检查 `NEW_YAMLS`（本次实际产物）：namespace、image、mount、resources、ports、
   workload identity，并确认没有源码 `PYTHONPATH`。
6. API 可用时，只对 `NEW_YAMLS` 做 `kubectl apply --dry-run=server -f ...`；排除
   deploy log、config copy 等非 YAML artifact。目标 namespace 不存在时停止；本 Skill
   不创建。

deployer dry-run 会写生成文件，但不得 apply 资源或修改用户 config。报告命令、生成
文件列表、检查结果、namespace/API 条件和 blockers；不生成 deploy-ready 标记。
