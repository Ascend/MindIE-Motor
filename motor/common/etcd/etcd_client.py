#!/usr/bin/env python3
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

# pylint: disable=no-member,no-name-in-module
# rpc_pb2 / rpc_pb2_grpc are generated protobuf modules inside an __init__.py
# namespace package; pylint cannot resolve their classes or the import itself.

import json
import os
import threading
from contextlib import contextmanager
from typing import Any, TypeVar

import grpc
from pydantic import BaseModel

from motor.config.etcd import EtcdConfig
from motor.config.tls_config import TLSConfig
from motor.common.etcd.proto import rpc_pb2, rpc_pb2_grpc
from motor.common.etcd import locks
from motor.common.logger import get_logger
from motor.common.utils.net import format_address


T = TypeVar('T', bound=BaseModel)

namespace = os.getenv("POD_NAMESPACE", "")
job_name = os.getenv("JOB_NAME", "")
logger = get_logger(__name__)
UTF8_ENCODING = "utf-8"
RB = 'rb'


class EtcdClient:
    """Etcd client with lease lock management and JSON data storage"""

    def __init__(self, etcd_config: EtcdConfig, tls_config: TLSConfig | None = None) -> None:
        self.tls_config = tls_config

        # Use etcd_config attributes as base configuration
        self.host = etcd_config.etcd_host
        self.port = etcd_config.etcd_port
        self.timeout = etcd_config.etcd_timeout
        self.channel = None
        self.kv_stub = None
        self.lease_stub = None
        self._leases: dict[str, int] = {}  # lock_key -> lease_id
        self._lock = threading.Lock()

        try:
            channel_options = [
                (
                    'grpc.service_config',
                    json.dumps(
                        {
                            "loadBalancingPolicy": etcd_config.etcd_lb_policy,
                        }
                    ),
                ),
            ]
            if self.tls_config and self.tls_config.enable_tls:
                with open(self.tls_config.ca_file, RB) as f:
                    root_cert = f.read()
                with open(self.tls_config.key_file, RB) as f:
                    private_key = f.read()
                with open(self.tls_config.cert_file, RB) as f:
                    cert_chain = f.read()
                creds = grpc.ssl_channel_credentials(
                    root_certificates=root_cert, private_key=private_key, certificate_chain=cert_chain
                )
                self.channel = grpc.secure_channel(format_address(self.host, self.port), creds, options=channel_options)
            else:
                self.channel = grpc.insecure_channel(format_address(self.host, self.port), options=channel_options)

            self.kv_stub = rpc_pb2_grpc.KVStub(self.channel)
            self.lease_stub = rpc_pb2_grpc.LeaseStub(self.channel)
        except Exception as e:
            logger.error("etcd_client init error: %s", e)
            if self.channel:
                self.channel.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @staticmethod
    def get_key_with_namespace_and_job_name(key: str) -> str:
        """key must start with /"""
        if key.startswith(namespace + "/" + job_name + "/"):
            return key
        return namespace + "/" + job_name + key

    @staticmethod
    def _prefix_range_end(prefix: str):
        """Create a bytestring that can be used as a range_end for a prefix."""
        bytes_prefix = prefix.encode(UTF8_ENCODING)
        s = bytearray(bytes_prefix)
        for i in reversed(range(len(s))):
            if s[i] < 0xFF:
                s[i] = s[i] + 1
                break
        return bytes(s)

    def acquire_lock(self, lock_key: str, ttl: int = 30) -> str | None:
        try:
            with self._lock:
                lock_key = self.get_key_with_namespace_and_job_name(lock_key)
                if lock_key in self._leases:
                    logger.debug("Lock %s already exists (held by another thread)", lock_key)
                    return None

                lock = locks.Lock(lock_key, ttl, etcd_client=self)
                success = lock.acquire(self.timeout)

                if success:
                    self._leases[lock_key] = lock.lease_id
                    logger.info("Acquired lock %s with lease %s", lock_key, lock.lease_id)
                    return str(lock.uuid)
                else:
                    logger.debug("Failed to acquire lock %s", lock_key)
                    return None
        except Exception as e:
            logger.error("Failed to acquire lock %s: %s", lock_key, e)
            return None

    def renew_lease(self, lock_key: str) -> bool:
        """Renew lease for a lock"""
        try:
            with self._lock:
                lock_key, lease_id = self._get_lock_key_and_id(lock_key)
                if lease_id is None:
                    logger.error("Lock %s does not exist", lock_key)
                    return False

                keep_alive_req = rpc_pb2.LeaseKeepAliveRequest(ID=lease_id)

                # 2. Call LeaseKeepAlive (this is a streaming API)
                # An iterator must be passed in, and the returned response iterator must be obtained.
                response_stream = self.lease_stub.LeaseKeepAlive(iter([keep_alive_req]), timeout=self.timeout)
                # 3. Read the response (this step is critical—it confirms that the lease renewal was successful
                #    and retrieves the latest TTL) If you don't read the next response, the request might still
                #    be buffered and not actually sent, or errors may go undetected.
                response = next(response_stream)
                new_ttl = response.TTL

                if new_ttl <= 0:
                    # If the returned TTL is <= 0, it means the lease has already expired.
                    raise RuntimeError(f"Lease expired (TTL={new_ttl}) during renewal")
                logger.debug("Renewed lease for lock %s. New TTL: %s", lock_key, new_ttl)
                return True
        except Exception as e:
            logger.error("Failed to renew lease for lock  %s: %s, will release", lock_key, e)
            self.release_lock(lock_key)
            return False

    def release_lock(self, lock_key: str) -> bool:
        """Release a lease lock"""
        try:
            with self._lock:
                lock_key, lease_id = self._get_lock_key_and_id(lock_key)
                if lease_id is None:
                    logger.warning("Lock %s does not exist", lock_key)
                    return False

                self.lease_stub.LeaseRevoke(rpc_pb2.LeaseRevokeRequest(ID=lease_id), timeout=self.timeout)
                del self._leases[lock_key]
                logger.debug("Released lock for lock %s", lock_key)
                return True
        except Exception as e:
            logger.error("Failed to release lock %s: %s", lock_key, e)
            del self._leases[lock_key]
            return False

    def put_json(self, key: str, data: BaseModel | dict[str, Any], lease: int = None) -> bool:
        """Store JSON data (pydantic compatible)"""
        try:
            if isinstance(data, BaseModel):
                json_data = data.model_dump_json()
            else:
                json_data = json.dumps(data, ensure_ascii=False)

            key = self.get_key_with_namespace_and_job_name(key)
            self._put_key_value(key, json_data, lease)

            logger.info("Stored JSON data for key %s", key)
            return True
        except Exception as e:
            logger.error("Error storing JSON data at key %s: %s", key, e)
            return False

    def delete_prefix(self, prefix: str) -> bool:
        """Delete all keys with given prefix"""
        try:
            prefix = self.get_key_with_namespace_and_job_name(prefix)
            resp = self.kv_stub.DeleteRange(
                rpc_pb2.DeleteRangeRequest(key=prefix.encode(UTF8_ENCODING), range_end=self._prefix_range_end(prefix)),
                timeout=self.timeout,
            )
            deleted_count = resp.deleted
            logger.info("Deleted %d keys with prefix %s", deleted_count, prefix)
            return True
        except Exception as e:
            logger.error("Failed to delete prefix %s: %s", prefix, e)
            return False

    def set_bool(self, key: str, value: bool, lease: int = None) -> bool:
        try:
            key = self.get_key_with_namespace_and_job_name(key)
            str_value = "true" if value else "false"
            self._put_key_value(key, str_value, lease)
            logger.debug("Stored bool %s for key %s", value, key)
            return True
        except Exception as e:
            logger.error("Error storing bool at key %s: %s", key, e)
            return False

    def get_bool(self, key: str, default: bool | None = False) -> bool | None:
        try:
            key = self.get_key_with_namespace_and_job_name(key)
            resp = self.kv_stub.Range(
                rpc_pb2.RangeRequest(key=key.encode(UTF8_ENCODING), limit=1), timeout=self.timeout
            )
            if not resp.kvs:
                return default
            str_value = resp.kvs[0].value.decode(UTF8_ENCODING).strip().lower()
            if str_value in ("true", "1"):
                return True
            if str_value in ("false", "0"):
                return False
            logger.warning("Invalid bool value for key %s: %s", key, str_value)
            return default
        except Exception as e:
            logger.error("Error getting bool at key %s: %s", key, e)
            return default

    def delete_key(self, key: str) -> bool:
        """Delete a specific key"""
        try:
            key = self.get_key_with_namespace_and_job_name(key)
            resp = self.kv_stub.DeleteRange(
                rpc_pb2.DeleteRangeRequest(key=key.encode(UTF8_ENCODING)), timeout=self.timeout
            )
            deleted_count = resp.deleted
            if deleted_count == 0:
                logger.info("key %s not found", key)
            else:
                logger.info("Deleted key %s", key)
            return True
        except Exception as e:
            logger.error("Failed to delete key %s: %s", key, e)
            return False

    @contextmanager
    def lock_context(self, lock_key: str, ttl: int = 30):
        """Context manager for automatic lock acquisition and release"""
        lease_id = None
        try:
            lease_id = self.acquire_lock(lock_key, ttl)
            if lease_id is None:
                raise RuntimeError(f"Failed to acquire lock {lock_key}")
            yield lease_id
        finally:
            if lease_id:
                if not self.release_lock(lock_key):
                    logger.error("Failed to release lock %s", lock_key)

    def close(self):
        """Close the proto client"""
        try:
            with self._lock:
                if self.channel:
                    self.channel.close()
            logger.info("EtcdClient closed")
        except Exception as e:
            logger.error("Failed to close EtcdClient: %s", e)

    def persist_data(self, key_prefix: str, data: dict[str, Any]) -> bool:
        """Persist data dictionary with key prefix"""
        try:
            with self.lock_context(f"persist_{key_prefix}", ttl=30):
                # Delete old data first
                self.delete_prefix(key_prefix)

                # Store new data
                for data_key, data_value in data.items():
                    full_key = f"{key_prefix}/{data_key}"
                    if not self.put_json(full_key, data_value):
                        logger.error("Failed to persist data for key %s", full_key)
                        return False
                logger.debug("Persisted %d items with prefix %s", len(data), key_prefix)
                return True

        except Exception as e:
            logger.debug("Failed to persist data with prefix %s: %s", key_prefix, e)
            return False

    def restore_data(self, key_prefix: str, model_class: type[T] | None = None) -> dict[str, dict[str, Any] | T] | None:
        """Restore data dictionary from key prefix"""
        try:
            data = self.get_prefix_data(key_prefix, model_class)
            if data:
                logger.debug("Restored %d items with prefix %s", len(data), key_prefix)
            else:
                # Return None when no data found (empty dict means no data)
                return None
            return data

        except Exception as e:
            logger.error("Failed to restore data with prefix %s: %s", key_prefix, e)
            return None

    def get_prefix_data(self, key_prefix: str, model_class: type[T] | None = None) -> dict[str, dict[str, Any] | T]:
        """Get all data under a prefix as dictionary"""
        data = {}
        try:
            key_prefix = self.get_key_with_namespace_and_job_name(key_prefix)
            resp = self.kv_stub.Range(
                rpc_pb2.RangeRequest(
                    key=key_prefix.encode(UTF8_ENCODING),
                    range_end=self._prefix_range_end(key_prefix),
                    limit=0,
                    keys_only=False,
                ),
                timeout=self.timeout,
            )

            logger.info("Getting %d items with prefix %s", len(resp.kvs), key_prefix)
            for kv_pair in resp.kvs:
                full_key_bytes = kv_pair.key
                full_key = full_key_bytes.decode(UTF8_ENCODING)

                prefix_len = len(key_prefix)
                relative_key = full_key[prefix_len + 1 :]  # skip '/'

                value_bytes = kv_pair.value  # gRPC return value is bytes type
                json_str = value_bytes.decode(UTF8_ENCODING)  # decode to JSON
                item_data = json.loads(json_str)

                if model_class:
                    data[relative_key] = model_class(**item_data)
                else:
                    data[relative_key] = item_data
            return data

        except Exception as e:
            logger.error("Failed to get prefix data with prefix %s: %s", key_prefix, e)
            return {}

    def _put_key_value(self, key: str, value: str, lease: int = None) -> None:
        """Helper method to put key-value pair with optional lease"""
        key_bytes = key.encode(UTF8_ENCODING)
        value_bytes = value.encode(UTF8_ENCODING)

        if lease:
            self.kv_stub.Put(rpc_pb2.PutRequest(key=key_bytes, value=value_bytes, lease=lease), timeout=self.timeout)
        else:
            self.kv_stub.Put(rpc_pb2.PutRequest(key=key_bytes, value=value_bytes), timeout=self.timeout)

    def _get_lock_key_and_id(self, lock_key: str) -> tuple[str, int | None]:
        """Helper method to get processed lock key and its lease ID"""
        lock_key = self.get_key_with_namespace_and_job_name(lock_key)
        lease_id = self._leases.get(lock_key)
        return lock_key, lease_id
