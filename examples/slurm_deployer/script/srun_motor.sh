#!/bin/bash
#SBATCH --ntasks-per-node=1
# 丢弃本脚本标准输出; 日志由 srun 按任务写入.
#SBATCH -o /dev/null
#SBATCH -e /dev/null

export LC_CTYPE=C.UTF-8
export ENABLE_IPC_HOST="enable"
export ROLE=$1
export CONTAINER_NAME="$ROLE"
export JOB_NAME=${2:-}

LOG_RUN_DIR="${LOG_RUN_DIR:-logs/$(date +%Y%m%d_%H%M%S)}"
mkdir -p "${LOG_RUN_DIR}/${ROLE}"

# 日志: %x 作业名, %j 作业号, %t 任务号, %N 节点名; 标准输出与错误合并.
exec srun --ntasks-per-node=1 \
     -o "${LOG_RUN_DIR}/${ROLE}/%x_%j_task%t_%N.log" \
     -e "${LOG_RUN_DIR}/${ROLE}/%x_%j_task%t_%N.log" \
     ./script/run_motor.sh
