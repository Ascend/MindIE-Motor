# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest
import threading
import logging

from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, Mock

import motor.controller.api_server.controller_api as controller_api
from motor.config.controller import ControllerConfig


@pytest.fixture(autouse=True)
def setup():
    """Setup TestClient for all tests"""
    pass  # The client is created in each test that needs it


@pytest.fixture
def client():
    """Create TestClient instance"""
    from motor.config.controller import ControllerConfig

    config = ControllerConfig()
    api_instance = controller_api.ControllerAPI(config)
    return TestClient(api_instance.app)


@pytest.fixture
def api_instance():
    from motor.config.controller import ControllerConfig

    config = ControllerConfig()
    config.standby_config = MagicMock()
    module = {}
    return controller_api.ControllerAPI(config, module)


@patch("motor.controller.api_server.controller_api.HeartbeatMsg")
@patch("motor.controller.api_server.controller_api.InstanceManager")
def test_heartbeat_success(mock_instance_manager, mock_heartbeat_msg, client) -> None:
    mock_heartbeat_msg.return_value = MagicMock()
    mock_instance_manager.return_value.handle_heartbeat.return_value = "ok"
    data = {"foo": "bar"}
    response = client.post("/controller/heartbeat", json=data)
    assert response.status_code == 200
    assert "result" in response.json()


@patch("motor.controller.api_server.controller_api.HeartbeatMsg", side_effect=Exception("parse error"))
def test_heartbeat_invalid(mock_heartbeat_msg, client) -> None:
    data = {"foo": "bar"}
    response = client.post("/controller/heartbeat", json=data)
    assert response.status_code == 200
    assert response.json()["error"] == "Invalid HeartbeatMsg format"


def test_get_controller_status_standalone_healthy(api_instance) -> None:
    # case1: standalone + all healthy (is_alive return true)
    standalone_config = ControllerConfig()
    standalone_config.standby_config.enable_master_standby = False
    api_instance_standalone = controller_api.ControllerAPI(standalone_config, api_instance.modules)

    healthy_module = MagicMock()
    healthy_module.is_alive.return_value = True
    api_instance_standalone.modules = {"module_a": healthy_module, "module_b": healthy_module}

    status = api_instance_standalone._get_controller_status()

    assert status["deploy_mode"] == "standalone"
    assert status["overall_healthy"] is True
    assert "role" not in status


def test_get_controller_status_standalone_unhealthy(api_instance) -> None:
    # case2: standalone + some healthy (is_alive return true or false)
    standalone_config = ControllerConfig()
    standalone_config.standby_config.enable_master_standby = False
    api_instance_standalone = controller_api.ControllerAPI(standalone_config, api_instance.modules)

    healthy_module = MagicMock()
    healthy_module.is_alive.return_value = True
    unhealthy_module = MagicMock()
    unhealthy_module.is_alive.return_value = False
    api_instance_standalone.modules = {"module_a": healthy_module, "module_b": unhealthy_module}

    status = api_instance_standalone._get_controller_status()

    assert status["deploy_mode"] == "standalone"
    assert status["overall_healthy"] is False
    assert "role" not in status


def test_get_controller_status_master_healthy(api_instance, monkeypatch) -> None:
    # case3: master_standby + master + all healthy (not have is_alive)
    master_config = ControllerConfig()
    master_config.standby_config.enable_master_standby = True
    api_instance.update_config(master_config)

    with patch("motor.controller.api_server.controller_api.StandbyManager") as mock_standby_cls:
        mock_standby_instance = MagicMock()
        mock_standby_cls.return_value = mock_standby_instance

        mock_standby_instance.is_master.return_value = True

        healthy_module = MagicMock()
        del healthy_module.is_alive
        api_instance.modules = {"module_a": healthy_module}

        status = api_instance._get_controller_status()

        assert status["deploy_mode"] == "master_standby"
        assert status["overall_healthy"] is True
        assert status["role"] == "master"


def test_get_controller_status_standby_healthy(api_instance) -> None:
    # case4: master_standby + standby + all unhealthy (is_alive return false)
    # Standby nodes should report healthy even if modules are not alive, since they don't run modules
    standby_config = ControllerConfig()
    standby_config.standby_config.enable_master_standby = True
    api_instance.update_config(standby_config)

    with patch("motor.controller.api_server.controller_api.StandbyManager") as mock_standby_cls:
        mock_standby_instance = MagicMock()
        mock_standby_cls.return_value = mock_standby_instance

        mock_standby_instance.is_master.return_value = False

        unhealthy_module = Mock()
        unhealthy_module.is_alive.return_value = False
        api_instance.modules = {"module_a": unhealthy_module}

        status = api_instance._get_controller_status()

        assert status["deploy_mode"] == "master_standby"
        assert status["overall_healthy"] is True  # Standby should always report healthy
        assert status["role"] == "standby"


@pytest.mark.asyncio
async def test_readiness_standalone_healthy(client, api_instance):
    """Test readiness in standalone mode with healthy modules"""
    api_instance._get_controller_status = Mock(return_value={"deploy_mode": "standalone", "overall_healthy": True})

    # Test the function directly - should not raise exception for healthy case
    result = await api_instance._readiness()
    # readiness() should return success message for successful case (200 status)
    assert result == {"message": "Controller is ready"}


@pytest.mark.asyncio
async def test_readiness_standalone_unhealthy(api_instance):
    """Test readiness in standalone mode with unhealthy modules"""
    api_instance._get_controller_status = Mock(
        return_value={
            "deploy_mode": "standalone",
            "overall_healthy": False,
        }
    )

    # Test the function directly - should raise HTTPException for unhealthy case
    with pytest.raises(HTTPException) as exc_info:
        await api_instance._readiness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["message"] == "Controller is not ready"
    assert exc_info.value.detail["reason"] == "Overall not healthy"


@pytest.mark.asyncio
async def test_readiness_master_standby_master_healthy(api_instance):
    """Test readiness in master_standby mode as master with healthy modules"""
    api_instance._get_controller_status = Mock(
        return_value={"deploy_mode": "master_standby", "role": "master", "overall_healthy": True}
    )

    # Test the function directly - should not raise exception for healthy master case
    result = await api_instance._readiness()
    assert result == {"message": "Controller is ready"}


@pytest.mark.asyncio
async def test_readiness_master_standby_master_unhealthy(api_instance):
    """Test readiness in master_standby mode as master with unhealthy modules"""
    api_instance._get_controller_status = Mock(return_value={"deploy_mode": "master_standby", "overall_healthy": False})

    # Test the function directly - should raise HTTPException for unhealthy case
    with pytest.raises(HTTPException) as exc_info:
        await api_instance._readiness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["message"] == "Controller is not ready"
    assert exc_info.value.detail["reason"] == "Overall not healthy"


@pytest.mark.asyncio
async def test_readiness_master_standby_standby_healthy(api_instance):
    """Test readiness in master_standby mode as standby with healthy modules"""
    api_instance._get_controller_status = Mock(return_value={"deploy_mode": "master_standby", "overall_healthy": True})

    # Test the function directly - should raise HTTPException for healthy standby case
    with pytest.raises(HTTPException) as exc_info:
        await api_instance._readiness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["message"] == "Controller is not ready"
    assert "Not master" in exc_info.value.detail["reason"]


@pytest.mark.asyncio
async def test_readiness_master_standby_standby_unhealthy(api_instance):
    """Test readiness in master_standby mode as standby with unhealthy modules"""
    api_instance._get_controller_status = Mock(return_value={"deploy_mode": "master_standby", "overall_healthy": False})

    # Test the function directly - should raise HTTPException for unhealthy case
    with pytest.raises(HTTPException) as exc_info:
        await api_instance._readiness()

    # When unhealthy, it should return unhealthy reason first
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["reason"] == "Overall not healthy"


@pytest.mark.asyncio
async def test_readiness_master_standby_invalid_role(api_instance):
    """Test readiness in master_standby mode with invalid role"""
    api_instance._get_controller_status = Mock(return_value={"deploy_mode": "master_standby", "overall_healthy": True})

    # Test the function directly - should raise HTTPException for invalid role case
    with pytest.raises(HTTPException) as exc_info:
        await api_instance._readiness()

    assert exc_info.value.status_code == 503
    assert "Not master" in exc_info.value.detail["reason"]


@pytest.mark.asyncio
async def test_readiness_missing_overall_healthy(api_instance):
    """Test readiness when overall_healthy key is missing"""
    api_instance._get_controller_status = Mock(
        return_value={
            "deploy_mode": "standalone"
            # missing overall_healthy
        }
    )

    # Test the function directly - should not raise exception when overall_healthy is missing (treated as healthy)
    result = await api_instance._readiness()
    assert result == {"message": "Controller is ready"}


@pytest.mark.asyncio
async def test_readiness_missing_deploy_mode(api_instance):
    """Test readiness when deploy_mode key is missing"""
    api_instance._get_controller_status = Mock(
        return_value={
            "overall_healthy": True
            # missing deploy_mode
        }
    )

    # Test the function directly - should not raise exception when deploy_mode is missing
    result = await api_instance._readiness()
    assert result == {"message": "Controller is ready"}


@pytest.mark.asyncio
async def test_startup_endpoint(api_instance):
    """Test startup endpoint returns correct message"""
    # Test the function directly instead of through HTTP
    result = await api_instance._startup()
    assert result == {"message": "Controller startup"}


@pytest.mark.asyncio
async def test_liveness_healthy(api_instance):
    """Test liveness when controller is healthy"""
    api_instance._get_controller_status = Mock(
        return_value={
            "overall_healthy": True,
            "deploy_mode": "standalone",
        }
    )

    # Test the function directly - should return result for healthy case
    result = await api_instance._liveness()
    assert result == {"message": "Controller is alive"}


@pytest.mark.asyncio
async def test_liveness_unhealthy(api_instance):
    """Test liveness when controller is unhealthy"""
    api_instance._get_controller_status = Mock(
        return_value={
            "overall_healthy": False,
            "deploy_mode": "standalone",
        }
    )

    # Test the function directly - should raise HTTPException for unhealthy case
    with pytest.raises(HTTPException) as exc_info:
        await api_instance._liveness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["message"] == "Controller is not alive"
    assert exc_info.value.detail["reason"] == "Overall not healthy"


@pytest.mark.asyncio
async def test_liveness_standby_mode(api_instance):
    """Test liveness in standby mode (should still be alive)"""
    api_instance._get_controller_status = Mock(
        return_value={
            "overall_healthy": True,
            "deploy_mode": "master_standby",
        }
    )

    # Test the function directly - should return result for healthy standby case
    result = await api_instance._liveness()
    assert result == {"message": "Controller is alive"}


@pytest.mark.asyncio
async def test_liveness_standby_unhealthy(api_instance):
    """Test liveness in standby mode when unhealthy"""
    api_instance._get_controller_status = Mock(
        return_value={
            "overall_healthy": False,
            "deploy_mode": "master_standby",
        }
    )

    # Test the function directly - should raise HTTPException for unhealthy case
    with pytest.raises(HTTPException) as exc_info:
        await api_instance._liveness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["message"] == "Controller is not alive"
    assert exc_info.value.detail["reason"] == "Overall not healthy"


@pytest.mark.asyncio
async def test_liveness_missing_overall_healthy(api_instance):
    """Test liveness when overall_healthy key is missing"""
    api_instance._get_controller_status = Mock(
        return_value={
            "deploy_mode": "standalone"
            # missing overall_healthy
        }
    )

    # Test the function directly - should return result when overall_healthy is missing (treated as healthy)
    result = await api_instance._liveness()
    assert result == {"message": "Controller is alive"}


@patch("motor.controller.api_server.controller_api.RegisterMsg")
@patch("motor.controller.api_server.controller_api.InstanceAssembler")
def test_register_success(mock_assembler, mock_register_msg, client) -> None:
    mock_register_msg.return_value = MagicMock()
    mock_assembler.return_value.register.return_value = "ok"
    data = {"foo": "bar"}
    response = client.post("/controller/register", json=data)
    assert response.status_code == 200
    assert "result" in response.json()


@patch("motor.controller.api_server.controller_api.RegisterMsg")
@patch("motor.controller.api_server.controller_api.InstanceAssembler")
def test_register_already_registered(mock_assembler, mock_register_msg, client) -> None:
    mock_register_msg.return_value = MagicMock()
    mock_assembler.return_value.register.return_value = -1
    data = {"foo": "bar"}
    response = client.post("/controller/register", json=data)
    assert response.status_code == 200
    assert response.json()["error"] == "Instance already registered"


@patch("motor.controller.api_server.controller_api.RegisterMsg", side_effect=Exception("parse error"))
def test_register_invalid(mock_register_msg, client) -> None:
    data = {"foo": "bar"}
    response = client.post("/controller/register", json=data)
    assert response.status_code == 200
    assert response.json()["error"] == "Invalid RegisterMsg format"


@patch("motor.controller.api_server.controller_api.ReregisterMsg")
@patch("motor.controller.api_server.controller_api.InstanceAssembler")
def test_reregister_success(mock_assembler, mock_reregister_msg, client) -> None:
    mock_reregister_msg.return_value = MagicMock()
    mock_assembler.return_value.reregister.return_value = "ok"
    data = {"foo": "bar"}
    response = client.post("/controller/reregister", json=data)
    assert response.status_code == 200
    assert "result" in response.json()


@patch("motor.controller.api_server.controller_api.ReregisterMsg")
@patch("motor.controller.api_server.controller_api.InstanceAssembler")
def test_reregister_already_registered(mock_assembler, mock_reregister_msg, client) -> None:
    mock_reregister_msg.return_value = MagicMock()
    mock_assembler.return_value.reregister.return_value = -1
    data = {"foo": "bar"}
    response = client.post("/controller/reregister", json=data)
    assert response.status_code == 200
    assert response.json()["error"] == "Instance already registered"


@patch("motor.controller.api_server.controller_api.ReregisterMsg", side_effect=Exception("parse error"))
def test_reregister_invalid(mock_reregister_msg, client) -> None:
    data = {"foo": "bar"}
    response = client.post("/controller/reregister", json=data)
    assert response.status_code == 200
    assert response.json()["error"] == "Invalid ReregisterMsg format"


def test_controller_api_thread_start() -> None:
    from motor.config.controller import ControllerConfig

    config = ControllerConfig()
    with patch.object(threading.Thread, "start") as mock_start:
        api = controller_api.ControllerAPI(config, "127.0.0.1", 12345)
        api.start()  # Starts API server thread and Observability API server thread
        assert mock_start.call_count == 2


def test_api_access_filter_non_uvicorn_access() -> None:
    """Test non uvicorn.access log should return True"""
    filter_instance = controller_api.ApiAccessFilter({"/controller/heartbeat": logging.ERROR})
    record = logging.LogRecord(
        name="other.logger", level=logging.INFO, pathname="", lineno=0, msg="some message", args=(), exc_info=None
    )
    assert filter_instance.filter(record) is True


def test_api_access_filter_uvicorn_access_non_filtered_path() -> None:
    """Test uvicorn.access log but not contain filtered path should return True"""
    filter_instance = controller_api.ApiAccessFilter({"/controller/heartbeat": logging.ERROR})
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "GET /status HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record) is True


def test_api_access_filter_uvicorn_access_filtered_path_info() -> None:
    """Test uvicorn.access log contain filtered path and level < configured level should return False"""
    filter_instance = controller_api.ApiAccessFilter({"/controller/heartbeat": logging.ERROR})
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record) is False


def test_api_access_filter_uvicorn_access_filtered_path_warning() -> None:
    """Test uvicorn.access log contain filtered path and level WARNING < ERROR should return False"""
    filter_instance = controller_api.ApiAccessFilter({"/controller/heartbeat": logging.ERROR})
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record) is False


def test_api_access_filter_uvicorn_access_filtered_path_error() -> None:
    """Test uvicorn.access log contain filtered path and level ERROR >= configured level should return True"""
    filter_instance = controller_api.ApiAccessFilter({"/controller/heartbeat": logging.ERROR})
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record) is True


def test_api_access_filter_uvicorn_access_filtered_path_critical() -> None:
    """Test uvicorn.access log contain filtered path and level CRITICAL >= configured level should return True"""
    filter_instance = controller_api.ApiAccessFilter({"/controller/heartbeat": logging.ERROR})
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.CRITICAL,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record) is True


def test_api_access_filter_getmessage_exception() -> None:
    """Test getMessage to raise exception should return True"""
    filter_instance = controller_api.ApiAccessFilter({"/controller/heartbeat": logging.ERROR})
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    # Mock getMessage to raise exception
    record.getMessage = MagicMock(side_effect=Exception("test exception"))
    assert filter_instance.filter(record) is True


def test_api_access_filter_multiple_paths() -> None:
    """Test filtering with multiple API paths configured"""
    api_filters = {
        "/controller/heartbeat": logging.ERROR,
        "/controller/register": logging.WARNING,
        "/controller/reregister": logging.INFO,
    }
    filter_instance = controller_api.ApiAccessFilter(api_filters)

    # Test heartbeat path with INFO level (should be filtered out since INFO < ERROR)
    record_heartbeat = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record_heartbeat) is False

    # Test register path with INFO level (should be filtered out since INFO < WARNING)
    record_register = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/register HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record_register) is False

    # Test register path with WARNING level (should be allowed through since WARNING >= WARNING)
    record_register_warning = logging.LogRecord(
        name="uvicorn.access",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/register HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record_register_warning) is True

    # Test reregister path with INFO level (should be allowed through since INFO >= INFO)
    record_reregister = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/reregister HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record_reregister) is True


def test_api_access_filter_empty_config() -> None:
    """Test filter with empty configuration allows all logs through"""
    filter_instance = controller_api.ApiAccessFilter({})

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record) is True


def test_api_access_filter_default_config() -> None:
    """Test filter with default (None) configuration allows all logs through"""
    filter_instance = controller_api.ApiAccessFilter()

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='127.0.0.1:12345 - "POST /controller/heartbeat HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record) is True


def test_update_config():
    """Test update_config method updates configuration fields"""
    from motor.config.controller import ControllerConfig

    # Create ControllerAPI instance
    config = ControllerConfig()
    api_instance = controller_api.ControllerAPI(config)

    # Create new config with different observability setting
    new_config = ControllerConfig()
    new_config.observability_config.observability_enable = True

    # Update config
    api_instance.update_config(new_config)

    # Verify observability config field was updated
    assert api_instance.enable_observability_api is True
