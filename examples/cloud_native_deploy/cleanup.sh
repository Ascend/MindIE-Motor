#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=${1:-}

if [[ -z "$NAMESPACE" ]]; then
  echo "Usage: $0 <namespace>" >&2
  exit 1
fi

if [[ "${MOTOR_DEPLOY_MODE:-}" == "rbg" ]]; then
  SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  WORKDIR=${WORKDIR:-"$SCRIPT_DIR/../deployer"}
  (
    cd "$WORKDIR"
    python3 - "$NAMESPACE" <<'PY'
import sys
from lib.rbg.lifecycle import cleanup_rbg

cleanup_rbg(sys.argv[1])
PY
  )
  echo "RBG cleanup completed for namespace: $NAMESPACE"
  exit 0
fi

echo "Cleaning resources in namespace: $NAMESPACE"

kubectl delete configmap motor-config -n "$NAMESPACE" --ignore-not-found=true

kubectl delete inferservicesets.mindcluster.huawei.com --all -n "$NAMESPACE" --ignore-not-found=true || true

kubectl delete deployment --all -n "$NAMESPACE" --ignore-not-found=true
kubectl delete service --all -n "$NAMESPACE" --ignore-not-found=true
kubectl delete serviceaccount mindie-motor-controller -n "$NAMESPACE" --ignore-not-found=true

kubectl delete clusterrolebinding "mindie-controller-binding-${NAMESPACE}" --ignore-not-found=true
kubectl delete clusterrole "mindie-controller-role-${NAMESPACE}" --ignore-not-found=true

echo "Cleanup completed for namespace: $NAMESPACE"
