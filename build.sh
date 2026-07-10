#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

set -euo pipefail

# This script builds the motor wheel package.

# Allow verbosity control: set VERBOSE=1 to see full logs.
VERBOSE=${VERBOSE:-0}

# Clean up any existing build artifacts that might cause import issues.
rm -rf build/
rm -rf motor.egg-info/
rm -rf dist/

echo "Generating protobuf files..."
./scripts/generate_proto.sh

touch ./motor/version.info
cat>./motor/version.info<<EOF
motor_version : 1.0.0
vllm_version : 0.13.0
vllm_ascend_version : 0.13.0
EOF

echo "Building wheel package with pip wheel (PEP517)... (VERBOSE=${VERBOSE})"

# Use pep517 build interface to avoid legacy setup.py warning. if no network, need add "--no-build-isolation"
cmd=(python -m pip wheel . --no-deps --use-pep517 -w dist -i https://pypi.tuna.tsinghua.edu.cn/simple)
if [[ "${VERBOSE}" -eq 0 ]]; then
  cmd+=(-q) # quiet output by default
fi

"${cmd[@]}"
