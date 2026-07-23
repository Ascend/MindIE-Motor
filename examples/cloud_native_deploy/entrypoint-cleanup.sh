#!/usr/bin/env bash
set -euo pipefail

WORKDIR=${WORKDIR:-/app/examples/deployer}
NAMESPACE=${NAMESPACE:-}

if [[ -z "$NAMESPACE" ]]; then
  echo "NAMESPACE is required for cleanup" >&2
  exit 1
fi

cd "$WORKDIR"

bash /app/examples/cloud_native_deploy/cleanup.sh "$NAMESPACE"
