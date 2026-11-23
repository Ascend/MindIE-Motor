# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Persistence envelope validation, independent of a running etcd server."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from motor.common.etcd.etcd_client import EtcdClient
from motor.common.etcd.persistent_state import PersistentState
from motor.config.etcd import EtcdConfig


@pytest.mark.parametrize("data", [{}, {"worker": {"ready": True, "ranks": [0, 1]}, "label": "中文"}])
def test_json_round_trip_preserves_integrity(data: dict) -> None:
    state = PersistentState(data=data, version=1, timestamp=1.0, checksum="")
    state.checksum = state.calculate_checksum()
    restored = PersistentState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert restored.is_valid()


def test_empty_state_checksum_matches_persisted_format() -> None:
    # Fixed SHA-256 vector for the persisted v1 envelope (empty data, timestamp 1).
    state = PersistentState(data={}, version=1, timestamp=1.0, checksum="")
    assert state.calculate_checksum() == "7eca367c997e979bbe53a60d25e58b21cd77c2742e701a3203de9d55969282bb"


@pytest.mark.parametrize(
    "field,value",
    [
        ("data", {"counter": 2}),
        ("version", 2),
        ("timestamp", 2.0),
        ("checksum", "corrupted"),
    ],
)
def test_changed_envelope_is_rejected(field: str, value: object) -> None:
    state = PersistentState(data={"counter": 1}, version=1, timestamp=1.0, checksum="")
    state.checksum = state.calculate_checksum()
    changed = state.model_copy(update={field: value})
    assert state.is_valid()
    assert not changed.is_valid()


@pytest.mark.parametrize("missing", ["data", "version", "timestamp", "checksum"])
def test_required_fields_are_validated(missing: str) -> None:
    payload = {"data": {}, "version": 1, "timestamp": 1.0, "checksum": "digest"}
    del payload[missing]
    with pytest.raises(ValidationError) as exc:
        PersistentState.model_validate(payload)
    assert exc.value.errors()[0]["loc"] == (missing,)


@pytest.mark.parametrize("field,value", [("data", []), ("version", "bad"), ("timestamp", "bad")])
def test_invalid_field_types_are_rejected(field: str, value: object) -> None:
    payload = {"data": {}, "version": 1, "timestamp": 1.0, "checksum": "digest", field: value}
    with pytest.raises(ValidationError):
        PersistentState.model_validate(payload)


def test_hash_failure_returns_empty_checksum_and_invalid_state() -> None:
    state = PersistentState(data={}, version=1, timestamp=1.0, checksum="saved-digest")
    with patch("motor.common.etcd.persistent_state.hashlib.sha256", side_effect=ValueError("unavailable")):
        assert state.calculate_checksum() == ""
        assert not state.is_valid()


def test_validation_exception_returns_false() -> None:
    state = PersistentState(data={}, version=1, timestamp=1.0, checksum="saved-digest")
    with patch.object(PersistentState, "calculate_checksum", side_effect=RuntimeError("failed")):
        assert not state.is_valid()


@pytest.fixture
def client():
    with (
        patch("motor.common.etcd.etcd_client.grpc.insecure_channel"),
        patch("motor.common.etcd.etcd_client.rpc_pb2_grpc.KVStub") as stub,
        patch("motor.common.etcd.etcd_client.rpc_pb2_grpc.LeaseStub"),
        EtcdClient(EtcdConfig()) as instance,
    ):
        yield instance, stub.return_value


def test_etcd_save_load_preserves_state_and_exposes_version_tampering(client) -> None:
    instance, stub = client
    state = PersistentState(data={"worker": "ready"}, version=7, timestamp=1.0, checksum="")
    state.checksum = state.calculate_checksum()
    lock = MagicMock()
    with patch.object(instance, "lock_context", return_value=lock) as acquire:
        assert instance.persist_data("/states", {"worker": state})
    acquire.assert_called_once_with("persist_/states", ttl=30)
    lock.__exit__.assert_called_once()
    request = stub.Put.call_args.args[0]
    assert request.key.endswith(b"/states/worker")
    assert PersistentState.model_validate_json(request.value) == state
    stub.Range.return_value.kvs = [SimpleNamespace(key=request.key, value=request.value)]
    restored = instance.restore_data("/states", PersistentState)["worker"]
    assert restored == state
    assert restored.is_valid()
    restored.version += 1
    assert not restored.is_valid()


@pytest.mark.parametrize("payload", [None, b'{"data":', b'{"data":{},"version":"bad"}'])
def test_etcd_missing_or_malformed_state_falls_back_to_none(client, payload: bytes | None) -> None:
    instance, stub = client
    key = instance.get_key_with_namespace_and_job_name("/states/worker").encode()
    stub.Range.return_value.kvs = [] if payload is None else [SimpleNamespace(key=key, value=payload)]
    assert instance.restore_data("/states", PersistentState) is None


def test_etcd_write_failure_releases_lock_and_returns_false(client) -> None:
    instance, stub = client
    stub.Put.side_effect = RuntimeError("etcd unavailable")
    lock = MagicMock()
    state = PersistentState(data={}, version=1, timestamp=1.0, checksum="digest")
    with patch.object(instance, "lock_context", return_value=lock):
        assert not instance.persist_data("/states", {"worker": state})
    lock.__exit__.assert_called_once()
