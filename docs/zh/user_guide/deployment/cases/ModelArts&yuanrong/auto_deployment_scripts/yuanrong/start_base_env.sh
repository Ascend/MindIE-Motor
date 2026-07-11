#!/bin/bash
MS_GLOBAL_RANKTABLE_TABLE=/user/global/config/global_rank_table.json
SCRIPT_PATH=/workspace/scripts

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

# git yuanrong patch
git config --global user.email "deploy@example.com"
git config --global user.name "deploy"
cd /vllm-workspace/vllm && git am $SCRIPT_PATH/yuanrong/install_packages/0001-Implement-yuanrong-backend.patch
cd /vllm-workspace/vllm && git am $SCRIPT_PATH/yuanrong/install_packages/0001-Bugfix-Fix-negative-local_cache_hit-in-P-D-disaggreg.patch
cd /vllm-workspace/vllm && git am $SCRIPT_PATH/yuanrong/install_packages/0001-fix-kv-pool-update-yuanrong-backend-handling.patch

# ETCD安装部署
cp $SCRIPT_PATH/yuanrong/install_packages/etcd-v3.5.10-linux-arm64/etcd /usr/local/bin/
cp $SCRIPT_PATH/yuanrong/install_packages/etcd-v3.5.10-linux-arm64/etcdctl /usr/local/bin/

ETCD_K8S_SERVICE=etcd-client-service.default.svc.cluster.local:2379

MAX_WAIT_S="${MAX_WAIT_S:-600}"
CHECK_INTERVAL_S="${CHECK_INTERVAL_S:-10}"

elapsed_s=0
while ((elapsed_s <= MAX_WAIT_S)); do
    if etcdctl --endpoints="${ETCD_K8S_SERVICE}" endpoint health >/dev/null 2>&1; then
        echo "etcd is ready: ${master_ip}"
        break
    fi

    if ((elapsed_s >= MAX_WAIT_S)); then
        echo "etcd not ready after ${MAX_WAIT_S}s: ${ETCD_ADDR}" >&2
        exit 1
    fi

    sleep_s=$((MAX_WAIT_S - elapsed_s))
    if ((sleep_s > CHECK_INTERVAL_S)); then
        sleep_s="${CHECK_INTERVAL_S}"
    fi
    echo "waiting for etcd... elapsed ${elapsed_s}s, max ${MAX_WAIT_S}s, next check in ${sleep_s}s"
    sleep "${sleep_s}"
    elapsed_s=$((elapsed_s + sleep_s))
done

# start yuanrong worker
pip install $SCRIPT_PATH/yuanrong/install_packages/openyuanrong_datasystem-0.8.1-cp311-cp311-manylinux_2_35_aarch64.whl
cd $SCRIPT_PATH/yuanrong
bash start_yr_worker.sh ${ETCD_K8S_SERVICE}