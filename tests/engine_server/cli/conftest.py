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

"""Install vllm module fakes before any cli test is collected.

Only vllm modules are faked globally — they are never imported by other
test suites and are safe to install at the conftest level.
Engine-server‑internal modules are handled by fixtures in the test file
to avoid global ``sys.modules`` pollution.
"""

import sys
from unittest.mock import MagicMock


def _install_vllm_fakes():
    """Install fake vllm modules so main.py can be imported."""
    fakes = {
        "vllm": MagicMock(),
        "vllm.entrypoints": MagicMock(),
        "vllm.entrypoints.openai": MagicMock(),
        "vllm.entrypoints.openai.cli_args": MagicMock(),
        "vllm.utils": MagicMock(),
        "vllm.utils.argparse_utils": MagicMock(),
    }
    for mod_name, fake in fakes.items():
        if mod_name not in sys.modules:
            sys.modules[mod_name] = fake


_install_vllm_fakes()
