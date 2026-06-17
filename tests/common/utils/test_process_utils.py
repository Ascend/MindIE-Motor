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

import multiprocessing
from unittest.mock import MagicMock, patch

from motor.common.utils.process_utils import set_process_title


def test_set_process_title_sets_logging_process_name():
    process = multiprocessing.current_process()
    original = process.name
    try:
        set_process_title("NodeManager")
        assert process.name == "NodeManager"
    finally:
        process.name = original


def test_set_process_title_calls_setproctitle():
    mock_module = MagicMock()
    with patch.dict("sys.modules", {"setproctitle": mock_module}):
        set_process_title("NodeManager")
    mock_module.setproctitle.assert_called_once_with("MindIE-Motor::NodeManager")


def test_set_process_title_import_error_no_raise():
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "setproctitle":
            raise ImportError("no setproctitle")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        set_process_title("NodeManager")
