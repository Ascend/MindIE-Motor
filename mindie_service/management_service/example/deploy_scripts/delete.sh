#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


echo -e "NOW EXECUTING [kubectl delete] COMMANDS. THE RESULT IS: \n\n"

NAME_SPACE=mindie
if [ -n "$1" ]; then
    NAME_SPACE="$1"
fi

kubectl delete cm boot-bash-script -n "$NAME_SPACE";
kubectl delete cm common-env -n "$NAME_SPACE";
kubectl delete cm global-ranktable -n "$NAME_SPACE";
kubectl delete cm python-script-get-group-id -n "$NAME_SPACE";
kubectl delete cm python-script-update-server-conf -n "$NAME_SPACE";
kubectl delete cm mindie-server-config -n "$NAME_SPACE";
kubectl delete cm mindie-ms-controller-config -n "$NAME_SPACE";
kubectl delete cm mindie-ms-coordinator-config -n "$NAME_SPACE";
kubectl delete cm mindie-ms-node-manager-config -n "$NAME_SPACE";
kubectl delete cm mindie-http-client-ctl-config -n "$NAME_SPACE";
kubectl delete cm config-file-path -n "$NAME_SPACE";
kubectl delete cm python-script-gen-config-single-container -n "$NAME_SPACE";
kubectl delete cm scaling-rule -n "$NAME_SPACE";
kubectl delete cm server-prestop-bash-script -n "$NAME_SPACE";
kubectl delete cm python-file-utils -n "$NAME_SPACE";

YAML_DIR=./output/deployment
if [ -n "$2" ]; then
    YAML_DIR="$2/deployment"
fi

for yaml_file in "$YAML_DIR"/*.yaml; do
    # 检查文件是否存在
    if [ -f "$yaml_file" ]; then
        # 权限校验
        python3 ./utils/file_utils.py "$yaml_file" --permission-mode 640 || exit 1
        # 使用kubectl delete命令删除每个YAML文件中定义的部署
        kubectl delete -f "$yaml_file"
    fi
done

# 将user_config.json和user_config_base_A3.json中的"model_id"字段设置为""
for file in ./*user_config*; do
    if [ -f "$file" ]; then
        sed -i -E 's/("model_id"\s*:\s*)"[^"]*"/\1""/g' "$file"
        echo "change $file model_id to empty"
    fi
done

# 检查boot文件权限
BOOT_FILE="./boot_helper/boot.sh"
if [ -f "$BOOT_FILE" ]; then
    python3 ./utils/file_utils.py "$BOOT_FILE" --permission-mode 640 --max-size 104857600 || exit 1
else
    echo "Boot file not found: $BOOT_FILE"
    exit 1
fi

sed -i '/^function set_controller_env()/,/^}/d' ./boot_helper/boot.sh
sed -i '/^function set_coordinator_env()/,/^}/d' ./boot_helper/boot.sh
sed -i '/^function set_prefill_env()/,/^}/d' ./boot_helper/boot.sh
sed -i '/^function set_decode_env()/,/^}/d' ./boot_helper/boot.sh
sed -i '/^function set_common_env()/,/^}/d' ./boot_helper/boot.sh
sed -i '/./,$!d' ./boot_helper/boot.sh