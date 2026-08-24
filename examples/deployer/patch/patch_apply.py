# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


"""Unified entry point for applying source patches to installed vLLM packages.

Orchestrates the per-domain patch appliers in this directory. Each applier
owns its version gate, patch specs and idempotency markers, so a new domain
only needs its own applier script plus a row in APPLIERS below.
"""

import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# (applier script, description)
APPLIERS = [
    ("patch_apply_shuffle_safetensors.py", "shuffle safetensors (weight loading)"),
    ("patch_apply_ascend_multi_connector.py", "AscendMultiConnector KV event proxy"),
]


def main() -> int:
    """Run every applier in order; return 0 when all pass or are skipped."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    failed = 0
    for script, description in APPLIERS:
        script_path = os.path.join(script_dir, script)
        if not os.path.isfile(script_path):
            logger.error("Patch applier not found: %s", script_path)
            failed += 1
            continue
        logger.info("Running patch applier: %s", description)
        result = subprocess.run([sys.executable, script_path], check=False)
        if result.returncode != 0:
            logger.error("Patch applier failed (%s): rc=%s", description, result.returncode)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
