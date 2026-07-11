# 获取集群节点信息
MS_GLOBAL_RANKTABLE_TABLE=/user/global/config/global_rank_table.json

while true; do
    json_string=$(cat $MS_GLOBAL_RANKTABLE_TABLE)
    echo $json_string;
    RESULT=$(jq -r '.status' $MS_GLOBAL_RANKTABLE_TABLE 2>/dev/null)
    echo $RESULT
    if [[ $RESULT = "completed" ]]; then
    echo "MA ranktable is completed";
    break;
    fi
    sleep 1;
done;

host_IP=$(hostname -I | xargs)
echo "host_IP = $host_IP"

NODE_IPS=$(jq -r '.server_group_list[].server_list[].server_ip' "$MS_GLOBAL_RANKTABLE_TABLE")
echo $NODE_IPS
IFS=$'\n' read -r -d '' -a ip_array <<< "$NODE_IPS"
master_ip="${ip_array[0]}"
echo "master_ip = $master_ip"

# 启动元戎worker
export HOST_IP=${host_IP}
export ETCD_K8S_SERVICE=$1
export WORKER_PORT=18481
export SHM_SIZE=512000
export NODE_TIMEOUT=300
export NODE_DEAD_TIMEOUT=600
export LIVENESS_PATH=/workspace/liveness
export DS_HOME_LOG="/mnt/cache/logs/ds/${HOST_IP}"

dsc1 start -t 600 -w \
    --worker_address "${HOST_IP}:${WORKER_PORT}" \
    --etcd_address "${ETCD_K8S_SERVICE}" \
    --cluster_name "etcd-glm5.1-EP" \
    --shared_memory_size_mb "${SHM_SIZE}" \
    --node_timeout_s "${NODE_TIMEOUT}" \
    --node_dead_timeout_s "${NODE_DEAD_TIMEOUT}" \
    --liveness_check_path "${LIVENESS_PATH}" \
    --log_dir "${DS_HOME_LOG}"

echo "yr worker start finished"
