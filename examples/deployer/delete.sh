#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

if [ -z "$1" ]; then
    echo "Usage: $0 <namespace>"
    echo "Example: $0 mindie-motor"
    exit 1
fi

NAMESPACE="$1"

if ! kubectl get namespace "$NAMESPACE" &>/dev/null; then
    echo "ERROR: Namespace '$NAMESPACE' does not exist"
    exit 1
fi

echo -e "NOW EXECUTING [kubectl delete] COMMANDS. THE RESULT IS: \n\n"
echo "Namespace: $NAMESPACE"

YAML_DIR=./output_yamls

# Step 1: Delete workloads first so pods enter graceful termination
# before their ConfigMap dependency is removed.
for yaml_file in "$YAML_DIR"/*.yaml; do
    if [ -f "$yaml_file" ]; then
        kubectl delete -f "$yaml_file"
    fi
done

# Step 2: Wait for all pods to be fully gone (will exit early if done sooner).
MAX_WAIT=30
for ((elapsed=0; elapsed<MAX_WAIT; elapsed++)); do
    remaining=$(kubectl get pods -n "$NAMESPACE" -o name --no-headers 2>/dev/null | wc -l)
    if [ "$remaining" -le 0 ]; then
        printf "\r\033[KAll pods terminated after %ds\n" "$elapsed"
        break
    fi
    printf "\r\033[KWaiting for %d pod(s) to terminate (%ds / %ds max)..." "$remaining" "$elapsed" "$MAX_WAIT"
    sleep 1
done
echo ""

# Fallback: force-delete any pod still stuck in Terminating after the wait above.
# (Terminating is not a status.phase value; stuck terminating pods have metadata.deletionTimestamp set.)
kubectl get pods -n "$NAMESPACE" -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' | while read -r pod; do
    [ -z "$pod" ] && continue
    echo "Force-deleting stuck terminating pod: $pod"
    kubectl delete pod "$pod" -n "$NAMESPACE" --force --grace-period=0
done

# Step 3: Delete ConfigMap after all workloads and pods have been cleaned up.
echo "Deleting ConfigMap motor-config in namespace $NAMESPACE"
kubectl delete cm motor-config -n "$NAMESPACE" --ignore-not-found

sed -i '/^# patch_begin/,/^# patch_end/d' ./startup/boot.sh
sed -i '/^function set_controller_env()/,/^}/d' ./startup/roles/controller.sh
sed -i '/^function set_coordinator_env()/,/^}/d' ./startup/roles/coordinator.sh
sed -i '/^function set_union_env()/,/^}/d' ./startup/roles/engine.sh
sed -i '/^function set_encode_env()/,/^}/d' ./startup/roles/engine.sh
sed -i '/^function set_prefill_env()/,/^}/d' ./startup/roles/engine.sh
sed -i '/^function set_decode_env()/,/^}/d' ./startup/roles/engine.sh
sed -i '/^function set_union_env()/,/^}/d' ./startup/roles/engine.sh
sed -i '/^function set_common_env()/,/^}/d' ./startup/common.sh
sed -i '/^function set_kv_store_env()/,/^}/d' ./startup/roles/kv_cache_store.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' ./startup/roles/kv_conductor.sh
sed -i '/^function set_controller_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_coordinator_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_encode_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_prefill_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_decode_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_union_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_kv_store_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' ./startup/roles/all_combine_in_single_container.sh
sed -i '/^function set_mf_store_env()/,/^}/d' ./startup/roles/mf_store.sh
sed -i '/./,$!d' ./startup/common.sh

echo "Delete completed."
