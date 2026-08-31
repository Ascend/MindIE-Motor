#!/bin/bash

clean_deployment() {
  if [ -e "$JOB_ID_FILE" ]; then
    echo "ERROR: 检测到 Job ID 记录，请先执行 bash deploy.sh stop，确认作业停止后再执行 clean。" >&2
    return 1
  fi
  rm -rf ./logs/*
  rm -rf ./kernel_meta/*
  rm -f /tmp/scheduler_frontend
  rm -f /dev/shm/coordinator_standby_role
  rm -f /tmp/scheduler_instance_pub
}
