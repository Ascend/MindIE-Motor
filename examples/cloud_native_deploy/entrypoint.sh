#!/usr/bin/env bash
set -euo pipefail

WORKDIR=${WORKDIR:-/app/examples/deployer}
CONFIG_DIR=${CONFIG_DIR:-/config}

cd "$WORKDIR"

python3 deploy.py --config_dir "$CONFIG_DIR" --nostep
