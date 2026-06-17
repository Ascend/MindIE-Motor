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

import importlib
from unittest.mock import MagicMock, patch


@patch("motor.config.node_manager.NodeManagerConfig.from_json", return_value=MagicMock())
@patch("motor.config.controller.ControllerConfig.from_json", return_value=MagicMock())
@patch("motor.common.utils.process_utils.set_process_title")
def test_module_sets_node_manager_title_on_import(mock_set_title, _mock_ctrl, _mock_nm):
    import motor.node_manager.main as nm_main

    importlib.reload(nm_main)

    mock_set_title.assert_called_with("NodeManager")


@patch("motor.node_manager.main.stop_all_modules")
@patch("motor.node_manager.main.log_config_summary")
@patch("motor.node_manager.main.init_all_modules")
def test_main_runs_without_resetting_process_title(mock_init, mock_log, mock_stop):
    import motor.node_manager.main as nm_main

    mock_config = MagicMock()
    mock_config.config_path = None
    nm_main.config = None
    nm_main._should_exit = False
    nm_main.config_watcher = None

    def fake_init(_path):
        nm_main.config = mock_config

    mock_init.side_effect = fake_init

    def fake_input():
        nm_main._should_exit = True
        return "stop"

    with patch("motor.node_manager.main.input", side_effect=fake_input):
        with patch("motor.node_manager.main.HeartbeatManager") as mock_hb:
            mock_hb.return_value.should_suicide.return_value = False
            nm_main.main()

    mock_init.assert_called_once()
