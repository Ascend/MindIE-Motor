#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Existing Windows clones may still expose shell scripts with CRLF endings
# after they receive .gitattributes. Avoid querying Git here because Docker
# Desktop bind mounts can trigger Git's cross-owner safe-directory check.
find . \
    -type d \( -name .git -o -name .venv -o -name node_modules -o -name target \) -prune \
    -o -type f -name '*.sh' -print0 \
    | xargs -0 -r sed -i 's/\r$//'

python -m pip install --requirement requirements.txt --editable .
bash scripts/generate_proto.sh
