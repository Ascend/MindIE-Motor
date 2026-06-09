# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from unittest.mock import MagicMock, patch

from motor.controller.core.recovery_service import terminate_instance_for_recovery


@patch("motor.controller.core.recovery_service.NodeManagerApiClient")
@patch("motor.controller.core.recovery_service.InstanceManager")
def test_recovery_returns_false_when_instance_missing(mock_im_cls, mock_nm) -> None:
    im = MagicMock()
    mock_im_cls.return_value = im
    im.get_instance.return_value = None
    assert terminate_instance_for_recovery(42, "reason") is False
    im.separate_instance.assert_not_called()
    mock_nm.stop.assert_not_called()


@patch("motor.controller.core.recovery_service.NodeManagerApiClient")
@patch("motor.controller.core.recovery_service.InstanceManager")
def test_recovery_separates_then_stops_node_managers(mock_im_cls, mock_nm) -> None:
    im = MagicMock()
    mock_im_cls.return_value = im
    instance = MagicMock()
    im.get_instance.return_value = instance
    nm = MagicMock()
    instance.get_node_managers.return_value = [nm]
    mock_nm.stop.return_value = True

    assert terminate_instance_for_recovery(7, "probe failed") is True
    im.separate_instance.assert_called_once_with(7)
    mock_nm.stop.assert_called_once_with(nm)
