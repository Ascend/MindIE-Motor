# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for :func:`motor.common.utils.process_utils.set_process_title`."""

import importlib
from unittest.mock import MagicMock, patch


# ===========================================================================
# NodeManager
# ===========================================================================


@patch("motor.config.node_manager.NodeManagerConfig.from_json", return_value=MagicMock())
@patch("motor.common.utils.process_utils.set_process_title")
def test_node_manager_sets_title_on_import(mock_set_title, _mock_cfg):
    import motor.node_manager.main as nm_main

    importlib.reload(nm_main)
    mock_set_title.assert_called_with("NodeManager")


@patch("motor.node_manager.main.run_port_setup_or_exit")
@patch("motor.node_manager.main.reconfigure_logging")
@patch("motor.node_manager.main.NodeManagerConfig")
def test_node_manager_main_does_not_reset_title(mock_cfg_cls, _mock_log, _mock_ports):
    mock_config = MagicMock()
    mock_cfg_cls.from_json.return_value = mock_config

    import motor.node_manager.main as nm_main

    with patch("motor.node_manager.main.NodeManager.run", return_value=0):
        with patch("motor.common.utils.process_utils.set_process_title") as mock_set_title:
            nm_main.main()

    # set_process_title was already called during import — main() must not re-call it
    mock_set_title.assert_not_called()
