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

# Keep motor_version in sync with motor/__init__.py::__version__ (single source of truth).
# Support both double- and single-quoted __version__ assignments (aligned with setup.py).
MOTOR_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' ./motor/__init__.py | head -n1)"
if [[ -z "${MOTOR_VERSION}" ]]; then
  MOTOR_VERSION="$(sed -n "s/^__version__[[:space:]]*=[[:space:]]*'\([^']*\)'.*/\1/p" ./motor/__init__.py | head -n1)"
fi
if [[ -z "${MOTOR_VERSION}" ]]; then
  echo "Error: failed to read __version__ from ./motor/__init__.py" >&2
  exit 1
fi

touch ./motor/version.info
cat>./motor/version.info<<EOF
motor_version : ${MOTOR_VERSION}
EOF
echo "Using motor_version=${MOTOR_VERSION}"

echo "Building wheel package with pip wheel (PEP517)... (VERBOSE=${VERBOSE})"

# Use pep517 build interface to avoid legacy setup.py warning. if no network, need add "--no-build-isolation"
cmd=(python -m pip wheel . --no-deps --use-pep517 -w dist -i https://pypi.tuna.tsinghua.edu.cn/simple)
if [[ "${VERBOSE}" -eq 0 ]]; then
  cmd+=(-q) # quiet output by default
fi

"${cmd[@]}"
