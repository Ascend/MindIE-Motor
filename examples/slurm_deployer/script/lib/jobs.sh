#!/bin/bash

submit_job() {
  local label="$1"
  shift
  local output job_id

  if ! output=$(sbatch --parsable "$@" 2>&1); then
    echo "ERROR: 提交 $label 作业失败: $output" >&2
    return 1
  fi
  job_id="${output%%;*}"
  if [[ ! "$job_id" =~ ^[0-9]+$ ]]; then
    echo "ERROR: 无法从 sbatch 输出中读取 $label 的 Job ID: $output" >&2
    return 1
  fi
  if ! printf '%s\n' "$job_id" >> "$JOB_ID_FILE"; then
    echo "ERROR: 无法记录 $label 的 Job ID: $JOB_ID_FILE" >&2
    return 1
  fi
  echo "Submitted $label, Job ID: $job_id"
}

stop_jobs() {
  local job_id failed=0
  if [ ! -s "$JOB_ID_FILE" ]; then
    echo "ERROR: 未找到本次部署的 Job ID 记录: $JOB_ID_FILE" >&2
    echo "请先执行 bash deploy.sh start，或确认记录文件未被清理。" >&2
    return 1
  fi

  while IFS= read -r job_id; do
    if [[ "$job_id" =~ ^[0-9]+$ ]]; then
      if scancel "$job_id"; then
        echo "Cancelled Job ID: $job_id"
      else
        echo "ERROR: 取消 Job ID $job_id 失败。" >&2
        failed=1
      fi
    else
      echo "ERROR: 忽略非法 Job ID: $job_id" >&2
      failed=1
    fi
  done < "$JOB_ID_FILE"

  [ "$failed" -eq 0 ] && rm -f "$JOB_ID_FILE"
  return "$failed"
}
