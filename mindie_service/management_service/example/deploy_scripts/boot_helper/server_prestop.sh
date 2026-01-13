#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


for config_file in "$MIES_INSTALL_PATH"/conf/config*.json; do
    if [ -f "$config_file" ]; then
        python3 /mnt/configmap/file_utils.py "$config_file" --permission-mode 640 --max-size 104857600 || exit 1
    fi
done
cat "$MIES_INSTALL_PATH"/conf/config*.json | grep managementPort
mapfile -t ports < <(cat "$MIES_INSTALL_PATH"/conf/config*.json | grep managementPort | awk -F':' '{print $NF}' | sed 's/,$//; s/^[[:space:]]*//; s/[[:space:]]*$//')
export HSECEASY_PATH="$MIES_INSTALL_PATH"/lib
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:"$MIES_INSTALL_PATH"/lib
export MINDIE_UTILS_HTTP_CLIENT_CTL_CONFIG_FILE_PATH="$MIES_INSTALL_PATH"/conf/http_client_ctl.json
for port in "${ports[@]}"; do
    echo "stop service $POD_IP:$port"

    hex_port=$(printf "%04X" "$port")
    if grep -q ":$hex_port" /proc/net/tcp; then
        python3 /mnt/configmap/file_utils.py "$MIES_INSTALL_PATH/bin/http_client_ctl" --permission-mode 550 || exit 1
        "$MIES_INSTALL_PATH"/bin/http_client_ctl "$POD_IP" "$port" "/stopService" 3 0 &
    else
        echo "port $port not occupied"
    fi
done
wait
sleep 30