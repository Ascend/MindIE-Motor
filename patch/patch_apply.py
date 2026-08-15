#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
import logging
import vllm


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def apply_patch(target_file: str, patch_file: str):
    cmd = ['patch', '-p0', '--fuzz=500', '--ignore-whitespace', target_file, patch_file]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        logger.info(f"Patch applied successfully to {target_file}")
    else:
        logger.error(f"Failed to apply patch to {target_file}")
        logger.error(result.stderr)


def patch_vllm_multi_connector(vllm_path: str, script_dir: str):
    target_file = f'{vllm_path[0]}/distributed/kv_transfer/kv_connector/v1/multi_connector.py'
    patch_file = os.path.join(script_dir, 'vllm_multi_connector.patch')

    apply_patch(target_file, patch_file)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vllm_path = vllm.__path__

    # Patch the multi_connector.py file in vllm to adapt pymotor for the layerwise superposition pooling feature.
    patch_vllm_multi_connector(vllm_path, script_dir)


if __name__ == '__main__':
    main()
