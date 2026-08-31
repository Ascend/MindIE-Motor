#!/bin/bash
set -euo pipefail

# 官方 MindIE Motor 镜像将 examples 放在 /tmp/motor/examples.
EXAMPLES_PATH=/tmp/motor/examples
if [ ! -d "$EXAMPLES_PATH/deployer/startup" ]; then
  echo "ERROR: 未找到 deployer/startup: $EXAMPLES_PATH/deployer/startup" >&2
  exit 1
fi
# The host conf directory is mounted at /conf. ConfigMap is instance-local.
CONFIGMAP_PATH="${CONFIGMAP_PATH:-/configmap}"
USER_CONFIG_PATH=/conf/user_config.json
ENV_PATH=/conf/env.json
STARTUP=$EXAMPLES_PATH/deployer/startup

if [ ! -f "$USER_CONFIG_PATH" ] || [ ! -f "$ENV_PATH" ]; then
  echo "ERROR: /conf/user_config.json and /conf/env.json are required" >&2
  exit 1
fi

mkdir -p "$CONFIGMAP_PATH"

cp -f "$STARTUP/boot.sh" "$CONFIGMAP_PATH/boot.sh"
cp -f "$STARTUP/common.sh" "$CONFIGMAP_PATH/common.sh"
cp -f "$STARTUP/hccl_tools.py" "$CONFIGMAP_PATH/hccl_tools.py"
cp -f "$STARTUP/roles/kv_store_backends/mooncake/mooncake_config.py" "$CONFIGMAP_PATH/mooncake_config.py" 2>/dev/null \
  || cp -f "$STARTUP/mooncake_config.py" "$CONFIGMAP_PATH/mooncake_config.py" 2>/dev/null \
  || true
cp -f "$STARTUP"/roles/*.sh "$CONFIGMAP_PATH"/
cp -f "$EXAMPLES_PATH/deployer/probe/probe.sh" "$CONFIGMAP_PATH/probe.sh" 2>/dev/null || true
cp -f "$EXAMPLES_PATH/deployer/probe/probe.py" "$CONFIGMAP_PATH/probe.py" 2>/dev/null || true
cp -f "$EXAMPLES_PATH/deployer/prestop/prestop.sh" "$CONFIGMAP_PATH/prestop.sh" 2>/dev/null || true
cp -f "$EXAMPLES_PATH/deployer/prestop/prestop.py" "$CONFIGMAP_PATH/prestop.py" 2>/dev/null || true
cp -f "$STARTUP/patch_inference_server.py" "$CONFIGMAP_PATH/patch_inference_server.py" 2>/dev/null || true

# kv_cache_store.sh 按 backend 查找 kv_store_backends.<backend>.<backend>.sh
cp -f "$STARTUP/roles/kv_store_backends/mooncake/mooncake.sh" \
    "$CONFIGMAP_PATH/kv_store_backends.mooncake.mooncake.sh" 2>/dev/null || true
cp -f "$STARTUP/roles/kv_store_backends/memcache/memcache.sh" \
    "$CONFIGMAP_PATH/kv_store_backends.memcache.memcache.sh" 2>/dev/null || true
cp -f "$STARTUP/roles/kv_store_backends/memcache/memcache_meta_service.py" \
    "$CONFIGMAP_PATH/kv_store_backends.memcache.memcache_meta_service.py" 2>/dev/null || true
cp -f "$STARTUP/roles/kv_store_backends/memcache/mmc-local-inprocess.conf" \
    "$CONFIGMAP_PATH/kv_store_backends.memcache.mmc-local-inprocess.conf" 2>/dev/null || true
cp -f "$STARTUP/roles/kv_store_backends/memcache/mmc-local-standalone.conf" \
    "$CONFIGMAP_PATH/kv_store_backends.memcache.mmc-local-standalone.conf" 2>/dev/null || true

# Copy all user-provided files from /conf after image defaults so users can
# override backend scripts or configuration files without a host ConfigMap.
while IFS= read -r -d '' user_file; do
  user_name="$(basename "$user_file")"
  [ "$user_name" = "prepare.sh" ] && continue
  cp -f "$user_file" "$CONFIGMAP_PATH/$user_name"
done < <(find /conf -maxdepth 1 -type f -print0)

# set_env_docker.py owns the complete function injection manifest. Slurm
# always uses the multi-container role layout, regardless of deploy_mode.

if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
"$PY" "$STARTUP/set_env_docker.py" --configmap_path "$CONFIGMAP_PATH"
