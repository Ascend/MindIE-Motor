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
from unittest.mock import mock_open, patch, MagicMock

import pytest
from pydantic import BaseModel

from motor.common.etcd import locks
from motor.common.etcd.etcd_client import EtcdClient
from motor.config.etcd import EtcdConfig
from motor.config.tls_config import TLSConfig


@pytest.fixture
def base_client_with_ssl():
    """Create base client with SSL."""
    mock_channel = MagicMock()
    mock_kv_stub = MagicMock()
    mock_lease_stub = MagicMock()

    tls_config = TLSConfig(
        enable_tls=True, ca_file="ca_cert_path", key_file="cert_key_path", cert_file="cert_cert_path"
    )

    # Create etcd_config object
    etcd_config = EtcdConfig(etcd_hosts=["test-host"], etcd_port=1234, etcd_timeout=10)

    with (
        patch("grpc.secure_channel", return_value=mock_channel),
        patch("motor.common.etcd.etcd_client.rpc_pb2_grpc.KVStub", return_value=mock_kv_stub),
        patch("motor.common.etcd.etcd_client.rpc_pb2_grpc.LeaseStub", return_value=mock_lease_stub),
        patch('builtins.open', mock_open(read_data=b"mock_content")),
    ):
        client = EtcdClient(etcd_config=etcd_config, tls_config=tls_config)
        return client


def test_init_default_parameters():
    """Test initialization with default parameters."""
    etcd_config = EtcdConfig()
    client = EtcdClient(etcd_config=etcd_config)
    assert client.hosts == ("etcd.default.svc.cluster.local",)
    assert client.port == 2379
    assert client.tls_config is None
    assert client.timeout == 5
    assert client.channel is not None
    assert client.kv_stub is not None
    assert client.lease_stub is not None
    assert client._leases == {}
    client.close()


@pytest.mark.parametrize(
    "hosts,target",
    [
        (["etcd.default.svc.cluster.local"], "etcd.default.svc.cluster.local:32379"),
        (["192.0.2.1"], "192.0.2.1:32379"),
        (["2001:db8::1"], "[2001:db8::1]:32379"),
        (["192.0.2.1", "192.0.2.2"], "ipv4:192.0.2.1:32379,192.0.2.2:32379"),
        (["2001:db8::1", "2001:db8::2"], "ipv6:[2001:db8::1]:32379,[2001:db8::2]:32379"),
        (["[2001:db8::1]", "[2001:db8::2]"], "ipv6:[2001:db8::1]:32379,[2001:db8::2]:32379"),
    ],
)
def test_static_hosts_use_one_grpc_channel_with_existing_load_balancing(hosts, target):
    """K8s DNS stays unchanged; static members become one native gRPC target."""
    with patch("grpc.insecure_channel") as channel:
        with EtcdClient(EtcdConfig(etcd_hosts=hosts, etcd_port=32379)):
            channel.assert_called_once()
            assert channel.call_args.args == (target,)
            options = dict(channel.call_args.kwargs["options"])
            assert json.loads(options["grpc.service_config"])["loadBalancingPolicy"] == "round_robin"
        channel.return_value.close.assert_called_once()


def test_init_with_certificates():
    """Test initialization with certificate files."""
    mock_channel = MagicMock()
    mock_kv_stub = MagicMock()
    mock_lease_stub = MagicMock()

    tls_config = TLSConfig(
        enable_tls=True, ca_file="ca_cert_path", key_file="cert_key_path", cert_file="cert_cert_path"
    )

    # Create etcd_config object
    etcd_config = EtcdConfig(etcd_hosts=["test-host"], etcd_port=1234, etcd_timeout=10)

    with (
        patch("grpc.secure_channel", return_value=mock_channel) as secure_channel,
        patch("motor.common.etcd.etcd_client.rpc_pb2_grpc.KVStub", return_value=mock_kv_stub),
        patch("motor.common.etcd.etcd_client.rpc_pb2_grpc.LeaseStub", return_value=mock_lease_stub),
        patch('builtins.open', mock_open(read_data=b"mock_content")),
    ):
        client = EtcdClient(etcd_config=etcd_config, tls_config=tls_config)

        assert client.hosts == ("test-host",)
        assert client.port == 1234
        assert client.tls_config == tls_config
        assert client.tls_config.ca_file == "ca_cert_path"
        assert client.tls_config.key_file == "cert_key_path"
        assert client.tls_config.cert_file == "cert_cert_path"
        assert client.timeout == 10
        assert client.channel == mock_channel
        assert client.kv_stub == mock_kv_stub
        assert client.lease_stub == mock_lease_stub
        assert secure_channel.call_args.args[0] == "test-host:1234"
        assert "grpc.default_authority" not in dict(secure_channel.call_args.kwargs["options"])


@pytest.mark.parametrize("hosts", [["192.0.2.1"], ["192.0.2.1", "192.0.2.2"]])
def test_tls_uses_explicit_service_identity_and_client_credentials(hosts):
    """An explicit identity applies to TLS only; switching it off preserves plaintext."""
    config = EtcdConfig(etcd_hosts=hosts, etcd_tls_server_name="motor-etcd.test")
    tls = TLSConfig(enable_tls=True, ca_file="ca.pem", cert_file="client.pem", key_file="client.key")
    with (
        patch("grpc.secure_channel") as secure,
        patch("grpc.insecure_channel") as insecure,
        patch("grpc.ssl_channel_credentials") as credentials,
        patch("builtins.open", mock_open(read_data=b"certificate")) as files,
    ):
        with EtcdClient(config, tls):
            credentials.assert_called_once_with(
                root_certificates=b"certificate", private_key=b"certificate", certificate_chain=b"certificate"
            )
            assert {call.args[0] for call in files.call_args_list} == {"ca.pem", "client.pem", "client.key"}
            options = dict(secure.call_args.kwargs["options"])
            assert options["grpc.default_authority"] == "motor-etcd.test"
            assert "grpc.ssl_target_name_override" not in options
            assert json.loads(options["grpc.service_config"])["loadBalancingPolicy"] == "round_robin"
            insecure.assert_not_called()

        files.reset_mock()
        tls.enable_tls = False
        with EtcdClient(config, tls):
            assert "grpc.default_authority" not in dict(insecure.call_args.kwargs["options"])
            files.assert_not_called()
            secure.assert_called_once()


def test_init_with_missing_certificates():
    """Test initialization with missing certificate files."""
    tls_config = TLSConfig(
        enable_tls=True, ca_file="ca_cert_path", key_file="cert_key_path", cert_file="cert_cert_path"
    )

    # Create etcd_config object
    etcd_config = EtcdConfig(etcd_hosts=["test-host"], etcd_port=1234, etcd_timeout=10)

    with patch("builtins.open", side_effect=FileNotFoundError("File not found")):
        client = EtcdClient(etcd_config=etcd_config, tls_config=tls_config)
        assert client.hosts == ("test-host",)


def test_get_key_with_namespace_and_job_name_already_has_namespace(monkeypatch):
    test_namespace = "test_namespace"
    monkeypatch.setattr("motor.common.etcd.etcd_client.namespace", test_namespace)
    job_name = "test_job_name"
    monkeypatch.setattr("motor.common.etcd.etcd_client.job_name", job_name)
    key = "test_namespace/test_job_name/key"
    result = EtcdClient.get_key_with_namespace_and_job_name(key)
    assert result == key


def test_get_key_with_namespace_and_job_name_no_prefix_has_namespace(monkeypatch):
    test_namespace = "test_namespace"
    monkeypatch.setattr("motor.common.etcd.etcd_client.namespace", test_namespace)
    key = "key"
    result = EtcdClient.get_key_with_namespace_and_job_name(key)
    assert result != key


def test_prefix_range_end_normal_case():
    """Test the normal case where the prefix does not end with 0xff."""
    prefix = "test"
    expected_result = b"tesu"  # "test" + 1 = "tesu"
    assert EtcdClient._prefix_range_end(prefix) == expected_result


def test_prefix_range_end_empty_string():
    """Test the case where the prefix is an empty string."""
    prefix = ""
    expected_result = b""  # "" + 1 = ""
    assert EtcdClient._prefix_range_end(prefix) == expected_result


def test_acquire_lock_success(base_client_with_ssl):
    with patch.object(locks, "Lock") as mock_lock_class:
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.lease_id = 12345
        mock_uuid_bytes = b"test_uuid_bytes"
        mock_lock.uuid = mock_uuid_bytes
        mock_lock_class.return_value = mock_lock

        result = base_client_with_ssl.acquire_lock("test_key")
        assert result is not None
        assert result == str(mock_uuid_bytes)


def test_acquire_lock_failure(base_client_with_ssl):
    with patch.object(locks, "Lock") as mock_lock_class:
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False  # Simulate acquire returning False
        mock_lock_class.return_value = mock_lock

        result = base_client_with_ssl.acquire_lock("test_key")

        assert result is None
        assert "test_key" not in base_client_with_ssl._leases


def test_acquire_lock_exception(base_client_with_ssl):
    with patch.object(locks, "Lock", side_effect=Exception("Test exception")):
        result = base_client_with_ssl.acquire_lock("test_key")

        assert result is None
        assert "test_key" not in base_client_with_ssl._leases


def test_renew_lease_success(base_client_with_ssl):
    # Set up mock behavior
    lock_key = "test_lock"
    lease_id = 123
    new_ttl = 60

    base_client_with_ssl._leases[lock_key] = lease_id
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=lock_key)

    # Mock LeaseKeepAlive response
    response = MagicMock()
    response.TTL = new_ttl
    response_stream = iter([response])
    base_client_with_ssl.lease_stub.LeaseKeepAlive.return_value = response_stream

    # Call method
    result = base_client_with_ssl.renew_lease(lock_key)

    # Assertions
    assert result is True
    base_client_with_ssl.lease_stub.LeaseKeepAlive.assert_called_once()


def test_renew_lease_lock_not_found(base_client_with_ssl):
    # Set up mock behavior
    lock_key = "test_lock"
    base_client_with_ssl.get_key_with_namespace_and_job_name.return_value = lock_key

    # Call method
    result = base_client_with_ssl.renew_lease(lock_key)

    # Assertions
    assert result is False
    base_client_with_ssl.lease_stub.LeaseKeepAlive.assert_not_called()


def test_renew_lease_expired(base_client_with_ssl):
    # Set up mock behavior
    lock_key = "test_lock"
    lease_id = 123
    new_ttl = 0

    base_client_with_ssl._leases[lock_key] = lease_id
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=lock_key)

    # Mock LeaseKeepAlive response
    response = MagicMock()
    response.TTL = new_ttl
    response_stream = iter([response])
    base_client_with_ssl.lease_stub.LeaseKeepAlive.return_value = response_stream

    # Call method
    result = base_client_with_ssl.renew_lease(lock_key)

    # Assertions
    assert result is False
    base_client_with_ssl.lease_stub.LeaseKeepAlive.assert_called_once()


def test_renew_lease_exception(base_client_with_ssl):
    # Set up mock behavior
    lock_key = "test_lock"
    lease_id = 123

    base_client_with_ssl._leases[lock_key] = lease_id
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=lock_key)

    # Simulate LeaseKeepAlive raising an exception
    base_client_with_ssl.lease_stub.LeaseKeepAlive.side_effect = Exception("Test exception")

    # Call method
    result = base_client_with_ssl.renew_lease(lock_key)

    # Assertions
    assert result is False
    base_client_with_ssl.lease_stub.LeaseKeepAlive.assert_called_once()


def test_release_lock_success(base_client_with_ssl):
    """Test successful lock release."""
    lock_key = "test_lock"
    lease_id = 12345
    base_client_with_ssl._leases[lock_key] = lease_id
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=lock_key)

    result = base_client_with_ssl.release_lock(lock_key)

    assert result is True
    assert lock_key not in base_client_with_ssl._leases


def test_release_lock_not_exist(base_client_with_ssl):
    """Test releasing a non-existent lock."""
    lock_key = "non_existent_lock"
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=lock_key)

    result = base_client_with_ssl.release_lock(lock_key)

    assert result is False
    assert lock_key not in base_client_with_ssl._leases


def test_release_lock_exception(base_client_with_ssl):
    """Test exception during lock release."""
    lock_key = "test_lock"
    lease_id = 12345
    base_client_with_ssl._leases[lock_key] = lease_id
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=lock_key)
    base_client_with_ssl.lease_stub.LeaseRevoke.side_effect = Exception("Test exception")

    result = base_client_with_ssl.release_lock(lock_key)

    assert result is False
    assert lock_key not in base_client_with_ssl._leases


@pytest.fixture
def mock_logger():
    with patch('motor.common.etcd.etcd_client.logger') as mock_logger:
        yield mock_logger


def test_put_json_with_pydantic_model(base_client_with_ssl, mock_logger):
    class TestModel(BaseModel):
        field: str

    model = TestModel(field="value")
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value="key")
    base_client_with_ssl.put_json("key", model)

    base_client_with_ssl.kv_stub.Put.assert_called_once()
    mock_logger.info.assert_called_once_with("Stored JSON data for key %s", "key")


def test_delete_prefix_success(base_client_with_ssl):
    # Simulate successful deletion
    mock_response = MagicMock()
    mock_response.deleted = 5
    base_client_with_ssl.kv_stub.DeleteRange.return_value = mock_response

    # Mock helper methods
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value="test_prefix")
    base_client_with_ssl._prefix_range_end = MagicMock(return_value="test_range_end".encode('utf-8'))

    # Call method
    result = base_client_with_ssl.delete_prefix("test_prefix")

    # Verify result
    assert result is True
    base_client_with_ssl.kv_stub.DeleteRange.assert_called_once()
    base_client_with_ssl.get_key_with_namespace_and_job_name.assert_called_once_with("test_prefix")
    base_client_with_ssl._prefix_range_end.assert_called_once_with("test_prefix")


def test_delete_prefix_failure(base_client_with_ssl):
    # Simulate deletion failure
    base_client_with_ssl.kv_stub.DeleteRange.side_effect = Exception("Delete failed")

    # Mock helper methods
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value="test_prefix")
    base_client_with_ssl._prefix_range_end = MagicMock(return_value="test_range_end".encode('utf-8'))

    # Call method
    result = base_client_with_ssl.delete_prefix("test_prefix")

    # Verify result
    assert result is False
    base_client_with_ssl.kv_stub.DeleteRange.assert_called_once()
    base_client_with_ssl.get_key_with_namespace_and_job_name.assert_called_once_with("test_prefix")
    base_client_with_ssl._prefix_range_end.assert_called_once_with("test_prefix")


def test_lock_context_success(base_client_with_ssl):
    """Test successful lock acquisition and release using context manager."""
    lease_id = "lease123"
    base_client_with_ssl.acquire_lock = MagicMock(return_value=lease_id)
    base_client_with_ssl.release_lock = MagicMock(return_value=True)

    with base_client_with_ssl.lock_context("test_lock") as acquired_lease_id:
        assert acquired_lease_id == lease_id

    base_client_with_ssl.release_lock.assert_called_once_with("test_lock")


def test_lock_context_acquire_failure(base_client_with_ssl):
    """Test failure to acquire lock using context manager."""
    base_client_with_ssl.acquire_lock = MagicMock(return_value=None)

    with pytest.raises(RuntimeError, match="Failed to acquire lock test_lock"):
        with base_client_with_ssl.lock_context("test_lock"):
            pass


def test_close_success(base_client_with_ssl):
    """Test that the close method successfully closes the channel."""
    mock_channel = MagicMock()
    mock_lock = MagicMock()
    base_client_with_ssl.channel = mock_channel
    base_client_with_ssl._lock = mock_lock

    base_client_with_ssl.close()

    mock_channel.close.assert_called_once()
    mock_lock.__enter__.assert_called_once()
    mock_lock.__exit__.assert_called_once()


def test_close_with_exception(base_client_with_ssl):
    """Test that the close method handles exceptions properly."""
    mock_channel = MagicMock()
    mock_lock = MagicMock()
    base_client_with_ssl.channel = mock_channel
    base_client_with_ssl._lock = mock_lock
    mock_channel.close.side_effect = Exception("Test exception")

    base_client_with_ssl.close()

    mock_channel.close.assert_called_once()
    mock_lock.__enter__.assert_called_once()
    mock_lock.__exit__.assert_called_once()


def test_persist_data_success(base_client_with_ssl):
    # Mock lock_context returning a context manager
    mock_lock_context = MagicMock()
    mock_lock_context.return_value.__enter__ = MagicMock(return_value=True)
    mock_lock_context.return_value.__exit__ = MagicMock()
    base_client_with_ssl.lock_context = mock_lock_context

    # Mock dependencies
    base_client_with_ssl.delete_prefix = MagicMock(return_value=True)
    base_client_with_ssl.put_json = MagicMock(return_value=True)

    # Test data
    key_prefix = "test_prefix"
    data = {"key1": {"value": "value1"}, "key2": {"value": "value2"}}

    # Call method
    result = base_client_with_ssl.persist_data(key_prefix, data)

    # Verify result
    assert result is True
    base_client_with_ssl.lock_context.assert_called_once_with(f"persist_{key_prefix}", ttl=30)
    base_client_with_ssl.delete_prefix.assert_called_once_with(key_prefix)
    assert base_client_with_ssl.put_json.call_count == len(data)
    for data_key, value in data.items():
        base_client_with_ssl.put_json.assert_any_call(f"{key_prefix}/{data_key}", value)


def test_persist_data_lock_failure(base_client_with_ssl):
    base_client_with_ssl.lock_context = MagicMock(side_effect=Exception("Lock failed"))

    # Mock dependencies
    base_client_with_ssl.delete_prefix = MagicMock(return_value=True)
    base_client_with_ssl.put_json = MagicMock(return_value=True)

    # Test data
    key_prefix = "test_prefix"
    data = {"key1": {"value": "value1"}, "key2": {"value": "value2"}}

    # Call method
    result = base_client_with_ssl.persist_data(key_prefix, data)

    # Verify result
    assert result is False
    base_client_with_ssl.lock_context.assert_called_once_with(f"persist_{key_prefix}", ttl=30)
    base_client_with_ssl.delete_prefix.assert_not_called()
    base_client_with_ssl.put_json.assert_not_called()


def test_persist_data_put_json_failure(base_client_with_ssl):
    # Mock lock_context returning a context manager
    mock_lock_context = MagicMock()
    mock_lock_context.return_value.__enter__ = MagicMock(return_value=True)
    mock_lock_context.return_value.__exit__ = MagicMock()
    base_client_with_ssl.lock_context = mock_lock_context

    # Mock dependencies
    base_client_with_ssl.delete_prefix = MagicMock(return_value=True)
    base_client_with_ssl.put_json = MagicMock(side_effect=[True, False])

    # Test data
    key_prefix = "test_prefix"
    data = {"key1": {"value": "value1"}, "key2": {"value": "value2"}}

    # Call method
    result = base_client_with_ssl.persist_data(key_prefix, data)

    # Verify result
    assert result is False
    base_client_with_ssl.lock_context.assert_called_once_with(f"persist_{key_prefix}", ttl=30)
    base_client_with_ssl.delete_prefix.assert_called_once_with(key_prefix)
    assert base_client_with_ssl.put_json.call_count == len(data)
    for data_key, value in data.items():
        base_client_with_ssl.put_json.assert_any_call(f"{key_prefix}/{data_key}", value)


def test_restore_data_success(base_client_with_ssl):
    """Test successful data restoration."""

    class TestRestoreModel(BaseModel):
        field1: str
        field2: int

    test_data = {"key1": {"field1": "value1", "field2": 1}, "key2": {"field1": "value2", "field2": 2}}

    with patch.object(EtcdClient, 'get_prefix_data', return_value=test_data):
        result = base_client_with_ssl.restore_data("test_prefix", TestRestoreModel)

    assert result == test_data


def test_restore_data_exception(base_client_with_ssl):
    """Test exception during data restoration."""

    class TestRestoreModel(BaseModel):
        field1: str
        field2: int

    with patch.object(EtcdClient, 'get_prefix_data', side_effect=Exception("Test exception")):
        result = base_client_with_ssl.restore_data("test_prefix", TestRestoreModel)

    assert result is None


def test_get_prefix_data_success(base_client_with_ssl):
    # Simulate successful data retrieval
    key_prefix = "test_prefix"
    mock_response = MagicMock()
    mock_response.kvs = [
        MagicMock(key=b"test_prefix/key1", value=json.dumps({"key1": "value1"}).encode("utf-8")),
        MagicMock(key=b"test_prefix/key2", value=json.dumps({"key2": "value2"}).encode("utf-8")),
    ]
    base_client_with_ssl.kv_stub.Range.return_value = mock_response
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=key_prefix)
    base_client_with_ssl._prefix_range_end.return_value = b"test_prefix0"

    result = base_client_with_ssl.get_prefix_data(key_prefix)

    assert result == {
        "key1": {"key1": "value1"},
        "key2": {"key2": "value2"},
    }
    base_client_with_ssl.kv_stub.Range.assert_called_once()


def test_get_prefix_data_with_model_class(base_client_with_ssl):
    # Simulate using a Pydantic model class
    class TestModel(BaseModel):
        key: str

    key_prefix = "test_prefix"
    mock_response = MagicMock()
    mock_response.kvs = [
        MagicMock(key=b"test_prefix/key1", value=json.dumps({"key": "value1"}).encode("utf-8")),
    ]
    base_client_with_ssl.kv_stub.Range.return_value = mock_response
    base_client_with_ssl.get_key_with_namespace_and_job_name = MagicMock(return_value=key_prefix)
    base_client_with_ssl._prefix_range_end.return_value = b"test_prefix0"

    result = base_client_with_ssl.get_prefix_data(key_prefix, model_class=TestModel)

    assert isinstance(result["key1"], TestModel)
    assert result["key1"].key == "value1"


def test_get_prefix_data_exception(base_client_with_ssl):
    # Simulate exception during data retrieval
    key_prefix = "test_prefix"
    base_client_with_ssl.kv_stub.Range.side_effect = Exception("Test exception")

    result = base_client_with_ssl.get_prefix_data(key_prefix)
    assert result == {}
