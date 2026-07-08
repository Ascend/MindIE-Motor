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
import sys
from unittest.mock import MagicMock, call, patch


@patch("motor.common.utils.process_utils.set_process_title")
def test_module_sets_engine_server_title_on_import(mock_set_title):
    old_argv = sys.argv
    try:
        sys.argv = ["engine_server", "--dp-rank", "2"]
        import motor.engine_server.cli.main as es_main

        importlib.reload(es_main)
        mock_set_title.assert_called_with("EngineServer-DP2")
    finally:
        sys.argv = old_argv


@patch("motor.engine_server.cli.main.setup_multiprocess_prometheus")
@patch("motor.engine_server.cli.main.EndpointFactory")
@patch("motor.engine_server.cli.main.ConfigFactory")
@patch("motor.config.endpoint.EndpointConfig.init_endpoint_config")
def test_main_runs_without_resetting_process_title(
    mock_init_endpoint_config,
    mock_config_factory_cls,
    mock_endpoint_factory_cls,
    mock_setup_prometheus,
):
    mock_endpoint_config = MagicMock()
    mock_endpoint_config.dp_rank = 2
    mock_endpoint_config.engine_type = "vllm"
    mock_endpoint_config.snapshot_metadata = None
    mock_init_endpoint_config.return_value = mock_endpoint_config

    mock_parsed_config = MagicMock()
    mock_config_factory_cls.return_value.parse.return_value = mock_parsed_config
    mock_infer_instance = MagicMock()
    mock_mgmt_instance = MagicMock()
    mock_endpoint_factory_cls.return_value.get_infer_endpoint.return_value = mock_infer_instance

    fake_infer_mod = MagicMock()
    fake_infer_mod.InferEndpoint = MagicMock(return_value=mock_infer_instance)
    fake_mgmt_mod = MagicMock()
    fake_mgmt_mod.MgmtEndpoint.return_value = mock_mgmt_instance

    with patch.dict(
        sys.modules,
        {
            "motor.engine_server.core.infer_endpoint": fake_infer_mod,
            "motor.engine_server.core.mgmt_endpoint": fake_mgmt_mod,
        },
    ):
        import motor.engine_server.cli.main as es_main

        es_main.main()

    mock_init_endpoint_config.assert_called_once()
    fake_mgmt_mod.MgmtEndpoint.assert_called_once_with(mock_endpoint_config)
    mock_mgmt_instance.run.assert_called_once()
    mock_config_factory_cls.return_value.parse.assert_called_once()
    mock_mgmt_instance.attach_engine.assert_called_once_with(mock_parsed_config)
    assert mock_mgmt_instance.method_calls.index(call.run()) < mock_mgmt_instance.method_calls.index(
        call.attach_engine(mock_parsed_config)
    )
