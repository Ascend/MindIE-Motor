#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


# 获取当前时间，格式为 YYYY-MM-DD_HH-MM-SS
time=$(date +"%Y-%m-%d_%H-%M-%S")
log_dir="./log/log_${time}"

# 创建日志目录
mkdir -p "$log_dir"

# 获取所有 mindie-server* 的 Pod：namespace name node
pods=$(kubectl get pods -A -o wide | grep "mindie" | awk '{print $1 " " $2 " " $8}')

# 检查是否找到匹配的 Pod
if [[ -z "$pods" ]]; then
    echo "未找到任何 mindie-server-xxxx 的 Pod"
    exit 1
fi

# 捕获中断信号，停止所有子进程
trap 'echo "停止日志记录..."; pkill -P $$ || true; exit 0' INT TERM

# 循环处理每个 Pod，异步记录日志
echo "$pods" | while read -r namespace podname nodename; do
    logfile="${log_dir}/${podname}_${nodename}.log"
    echo "正在记录 Pod [$podname] (Namespace: $namespace) 的日志到 $logfile"
    kubectl logs -f -n "$namespace" "$podname" > "$logfile" 2>&1 &
done

echo "日志记录启动完成。日志保存在 $log_dir"