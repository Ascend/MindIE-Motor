# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

from motor.engine_server.core.snapshot_monitor import SnapshotMonitor
from motor.engine_server.core.snapshot_sentinel import SnapshotSentinel


def setup_function():
    if hasattr(SnapshotMonitor, "_instances") and SnapshotMonitor in SnapshotMonitor._instances:
        del SnapshotMonitor._instances[SnapshotMonitor]


def teardown_function():
    if hasattr(SnapshotMonitor, "_instances") and SnapshotMonitor in SnapshotMonitor._instances:
        del SnapshotMonitor._instances[SnapshotMonitor]


def _create_snapshot_metadata_file(checkpoint: str | None = None):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        data = {
            "model_save_path": "/snapshot/weight",
            "model_load_path": "/snapshot/weight",
            "data_parallel_master_ip": "10.0.0.1",
        }
        if checkpoint is not None:
            data["checkpoint"] = checkpoint
        json.dump(data, f)
        return f.name


def _make_endpoint_config(metadata_path):
    deploy_config = MagicMock()
    deploy_config.infer_tls_config = None
    deploy_config.health_check_config = MagicMock()
    deploy_config.health_check_config.health_collector_timeout = 1.0

    config = MagicMock()
    config.snapshot_metadata = metadata_path
    config.port = 8000
    config.deploy_config = deploy_config
    return config


def _mock_http_client(mock_client_cls):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = b"true"
    mock_client.do_get.return_value = mock_response
    mock_client.do_post.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client_cls.return_value = mock_client
    return mock_client


@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
def test_wait_until_infer_healthy_success(mock_client_cls, _mock_pod_ip):
    metadata_path = _create_snapshot_metadata_file()
    try:
        _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel._wait_until_infer_healthy()
        mock_client_cls.assert_called_once()
        mock_client_cls.return_value.do_get.assert_called_once_with("health")
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_call_suspend_success(mock_sleep, mock_client_cls, _mock_pod_ip):
    metadata_path = _create_snapshot_metadata_file()
    try:
        mock_client = _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel._call_suspend()
        mock_client.do_post.assert_called_once_with(
            "suspend",
            query_params={"model_save_path": "/snapshot/weight"},
        )
        assert SnapshotMonitor().is_suspend_done is True
        mock_sleep.assert_not_called()
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_call_resume_success(mock_sleep, mock_client_cls, _mock_pod_ip):
    metadata_path = _create_snapshot_metadata_file()
    try:
        mock_client = _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel._call_resume()
        mock_client.do_post.assert_called_once_with(
            "resume",
            query_params={
                "model_path": "/snapshot/weight",
                "data_parallel_master_ip": "10.0.0.1",
            },
        )
        assert SnapshotMonitor().is_resume_done is True
        mock_sleep.assert_not_called()
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_call_suspend_retries_until_success(mock_sleep, mock_client_cls, _mock_pod_ip):
    metadata_path = _create_snapshot_metadata_file()
    try:
        mock_client = _mock_http_client(mock_client_cls)
        mock_client.do_post.side_effect = [RuntimeError("temporary failure"), None]
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel._call_suspend()
        assert mock_client.do_post.call_count == 2
        assert SnapshotMonitor().is_suspend_done is True
        mock_sleep.assert_called_once()
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_reach_checkpoint_unlocks_and_stops_on_cold_start(mock_sleep, mock_client_cls, _mock_pod_ip, _mock_restored):
    metadata_path = _create_snapshot_metadata_file(checkpoint="done")
    try:
        mock_client = _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel._reach_checkpoint()
        mock_client.do_post.assert_called_once_with("device_unlock")
        assert sentinel._stop_event.is_set()
        mock_sleep.assert_not_called()
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_reach_checkpoint_retries_when_checkpoint_not_done(mock_sleep, mock_client_cls, _mock_pod_ip, _mock_restored):
    metadata_path = _create_snapshot_metadata_file(checkpoint="pending")
    try:
        mock_client = _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))

        def stop_after_second_retry(_interval):
            if mock_sleep.call_count >= 2:
                sentinel.stop()

        mock_sleep.side_effect = stop_after_second_retry
        sentinel._reach_checkpoint()
        assert mock_client.do_post.call_count == 0
        assert mock_sleep.call_count == 2
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_reach_checkpoint_retries_when_checkpoint_field_missing(
    mock_sleep, mock_client_cls, _mock_pod_ip, _mock_restored
):
    metadata_path = _create_snapshot_metadata_file()
    try:
        mock_client = _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))

        def stop_after_second_retry(_interval):
            if mock_sleep.call_count >= 2:
                sentinel.stop()

        mock_sleep.side_effect = stop_after_second_retry
        sentinel._reach_checkpoint()
        assert mock_client.do_post.call_count == 0
        assert mock_sleep.call_count == 2
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.is_restored_from_host_side_snapshot", return_value=True)
@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_reach_checkpoint_skips_loop_when_restored(mock_sleep, mock_client_cls, _mock_pod_ip, _mock_restored):
    metadata_path = _create_snapshot_metadata_file(checkpoint="done")
    try:
        _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel._reach_checkpoint()
        mock_client_cls.assert_not_called()
        mock_sleep.assert_not_called()
        assert not sentinel._stop_event.is_set()
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.snapshot_sentinel.SnapshotSentinel._call_resume")
@patch("motor.engine_server.core.snapshot_sentinel.SnapshotSentinel._call_suspend")
@patch("motor.engine_server.core.snapshot_sentinel.SnapshotSentinel._wait_until_infer_healthy")
@patch("motor.engine_server.core.snapshot_sentinel.get_pod_ip", return_value="127.0.0.1")
@patch("motor.engine_server.core.snapshot_sentinel.SafeHTTPSClient")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_run_stops_after_checkpoint_without_resume(
    mock_sleep,
    mock_client_cls,
    _mock_pod_ip,
    mock_wait_healthy,
    mock_call_suspend,
    mock_call_resume,
    _mock_restored,
):
    metadata_path = _create_snapshot_metadata_file(checkpoint="done")
    try:
        _mock_http_client(mock_client_cls)
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel.run()
        mock_wait_healthy.assert_called_once()
        mock_call_suspend.assert_called_once()
        mock_call_resume.assert_not_called()
        assert sentinel._stop_event.is_set()
    finally:
        os.unlink(metadata_path)


@patch("motor.engine_server.core.snapshot_sentinel.is_restored_from_host_side_snapshot", return_value=True)
@patch("motor.engine_server.core.snapshot_sentinel.SnapshotSentinel._call_resume")
@patch("motor.engine_server.core.snapshot_sentinel.SnapshotSentinel._call_suspend")
@patch("motor.engine_server.core.snapshot_sentinel.SnapshotSentinel._wait_until_infer_healthy")
@patch("motor.engine_server.core.snapshot_sentinel.time.sleep")
def test_run_executes_suspend_then_resume(
    mock_sleep,
    mock_wait_healthy,
    mock_call_suspend,
    mock_call_resume,
    _mock_restored,
):
    metadata_path = _create_snapshot_metadata_file()
    try:
        sentinel = SnapshotSentinel(_make_endpoint_config(metadata_path))
        sentinel.run()
        mock_wait_healthy.assert_called_once()
        mock_call_suspend.assert_called_once()
        mock_call_resume.assert_called_once()
    finally:
        os.unlink(metadata_path)
