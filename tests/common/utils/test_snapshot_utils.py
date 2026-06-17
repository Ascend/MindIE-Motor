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

import pytest

from motor.common.utils import snapshot_utils


def _create_metadata_file(data=None):
    payload = data or {"job_name": "restored-job", "namespace": "new-ns"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f)
        return f.name


def test_is_restored_from_host_side_snapshot():
    with patch("motor.common.utils.snapshot_utils.os.path.exists", return_value=True):
        assert snapshot_utils.is_restored_from_host_side_snapshot() is True

    with patch("motor.common.utils.snapshot_utils.os.path.exists", return_value=False):
        assert snapshot_utils.is_restored_from_host_side_snapshot() is False


def test_load_snapshot_metadata_success():
    path = _create_metadata_file()
    try:
        assert snapshot_utils.load_snapshot_metadata(path, "job_name") == "restored-job"
        assert snapshot_utils.load_snapshot_metadata(path, "namespace") == "new-ns"
    finally:
        os.unlink(path)


def test_load_snapshot_metadata_file_not_found():
    with pytest.raises(FileNotFoundError):
        snapshot_utils.load_snapshot_metadata("/tmp/nonexistent_snapshot_metadata.json", "job_name")


def test_load_snapshot_metadata_invalid_json():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write("{invalid")
        path = f.name
    try:
        with pytest.raises(ValueError, match="not valid JSON"):
            snapshot_utils.load_snapshot_metadata(path, "job_name")
    finally:
        os.unlink(path)


def test_load_snapshot_metadata_non_object_root():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(["not", "an", "object"], f)
        path = f.name
    try:
        with pytest.raises(ValueError, match="must be an object"):
            snapshot_utils.load_snapshot_metadata(path, "job_name")
    finally:
        os.unlink(path)


def test_load_snapshot_metadata_invalid_field_type():
    path = _create_metadata_file()
    try:
        with pytest.raises(ValueError, match="requires string field"):
            snapshot_utils.load_snapshot_metadata(path, "missing_field")
    finally:
        os.unlink(path)


def test_update_snapshot_metadata():
    path = _create_metadata_file()
    try:
        snapshot_utils.update_snapshot_metadata(path, "model_save_path", "/snapshot/weight")
        assert snapshot_utils.load_snapshot_metadata(path, "model_save_path") == "/snapshot/weight"
    finally:
        os.unlink(path)


def test_update_snapshot_metadata_file_not_found():
    with pytest.raises(FileNotFoundError):
        snapshot_utils.update_snapshot_metadata("/tmp/nonexistent_snapshot_metadata.json", "job_name", "value")


@patch("motor.common.utils.snapshot_utils.socket.socket")
def test_get_pod_ip_success(mock_socket_cls):
    mock_socket = MagicMock()
    mock_socket.getsockname.return_value = ("10.0.0.5", 12345)
    mock_socket_cls.return_value = mock_socket

    assert snapshot_utils.get_pod_ip() == "10.0.0.5"
    mock_socket.connect.assert_called_once()


@patch("motor.common.utils.snapshot_utils.socket.socket")
def test_get_pod_ip_raises_when_all_targets_fail(mock_socket_cls):
    mock_socket = MagicMock()
    mock_socket.connect.side_effect = OSError("network unreachable")
    mock_socket_cls.return_value = mock_socket

    with pytest.raises(RuntimeError, match="Failed to detect pod IP"):
        snapshot_utils.get_pod_ip()
