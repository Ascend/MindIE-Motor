# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import subprocess
import sys
import logging
import vllm


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def apply_patch(target_file: str, patch_file: str):
    cmd = ['patch', '-p0', '--fuzz=500', '--ignore-whitespace', target_file, patch_file]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode == 0:
        logger.info(f"Patch applied successfully to {target_file}")
    else:
        logger.error(f"Failed to apply patch to {target_file}")
        logger.error(result.stderr)


def apply_patch_dir(target_dir: str, patch_file: str):
    """Apply a multi-file unified patch with paths relative to target_dir.

    Skips silently when the patch is already applied (reverse dry-run
    succeeds), but raises on a real application failure: the anthropic
    serving patch is load-bearing for the PD-separated deployment, and a
    silent miss surfaces later as hung requests or wrong responses.
    """
    cmd = ['patch', '-p0', '--fuzz=500', '--ignore-whitespace', '-d', target_dir, '-i', patch_file]

    already = subprocess.run(cmd + ['--dry-run', '-R'], capture_output=True, text=True, check=False)
    if already.returncode == 0:
        logger.info(f"Patch already applied under {target_dir}; skipping")
        return

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode == 0:
        logger.info(f"Patch applied successfully under {target_dir}")
    else:
        logger.error(f"Failed to apply patch under {target_dir}")
        logger.error(result.stderr)
        raise RuntimeError(
            f"Failed to apply patch {os.path.basename(patch_file)} under {target_dir}: {result.stderr.strip()}"
        )


def patch_vllm_multi_connector(vllm_path: str, script_dir: str):
    target_file = f'{vllm_path[0]}/distributed/kv_transfer/kv_connector/v1/multi_connector.py'
    patch_file = os.path.join(script_dir, 'vllm_multi_connector.patch')

    apply_patch(target_file, patch_file)


def patch_vllm_anthropic_serving(vllm_path: str, script_dir: str):
    patch_file = os.path.join(script_dir, 'vllm_anthropic_serving.patch')

    apply_patch_dir(vllm_path[0], patch_file)

    # A fuzz-misapplied hunk can still exit 0 while corrupting the target
    # (wrong anchor match). Syntax-check the patched files so a bad apply
    # fails loudly here instead of degrading the endpoints to 501 at runtime.
    for rel in ('entrypoints/anthropic/protocol.py', 'entrypoints/anthropic/serving.py'):
        target = os.path.join(vllm_path[0], rel)
        result = subprocess.run(
            [sys.executable, '-m', 'py_compile', target], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Patch {os.path.basename(patch_file)} left {rel} syntactically invalid: {result.stderr.strip()}"
            )


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vllm_path = vllm.__path__

    # Patch the multi_connector.py file in vllm to adapt pymotor for the layerwise superposition pooling feature.
    patch_vllm_multi_connector(vllm_path, script_dir)

    # Patch the anthropic serving in vllm: kv_transfer_params/request_id fields,
    # usage cache fields, tool-call arguments guard, and tool_use stop_reason.
    patch_vllm_anthropic_serving(vllm_path, script_dir)


if __name__ == '__main__':
    main()
