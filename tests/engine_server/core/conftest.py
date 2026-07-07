# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Install vllm mocks before core engine_server tests are collected."""

import sys
from unittest.mock import MagicMock


def _install_vllm_mocks() -> None:
    if "vllm" in sys.modules:
        return

    mock_vllm = MagicMock()
    mock_vllm.entrypoints = MagicMock()
    mock_vllm.entrypoints.openai = MagicMock()
    mock_vllm.entrypoints.openai.cli_args = MagicMock()
    mock_vllm.entrypoints.openai.cli_args.make_arg_parser = MagicMock()
    mock_vllm.entrypoints.openai.cli_args.validate_parsed_serve_args = MagicMock()
    mock_request_logger_mod = MagicMock()
    mock_request_logger_mod.RequestLogger = MagicMock()
    mock_api_utils_mod = MagicMock()
    mock_api_utils_mod.process_lora_modules = MagicMock()
    mock_api_utils_mod.cli_env_setup = MagicMock()
    sys.modules["vllm"] = mock_vllm
    sys.modules["vllm.entrypoints"] = mock_vllm.entrypoints
    sys.modules["vllm.entrypoints.openai"] = mock_vllm.entrypoints.openai
    sys.modules["vllm.entrypoints.openai.cli_args"] = mock_vllm.entrypoints.openai.cli_args
    sys.modules["vllm.entrypoints.serve"] = MagicMock()
    sys.modules["vllm.entrypoints.serve.utils"] = MagicMock()
    sys.modules["vllm.entrypoints.serve.utils.request_logger"] = mock_request_logger_mod
    sys.modules["vllm.entrypoints.serve.utils.api_utils"] = mock_api_utils_mod
    sys.modules["vllm.utils"] = MagicMock()
    sys.modules["vllm.utils.argparse_utils"] = MagicMock()


_install_vllm_mocks()
