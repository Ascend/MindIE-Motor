# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from .constants import ALG_AES_CTR_128


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value, 0)


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def _session_id(name: str, default_random: bool = True) -> int:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        if default_random:
            sid = secrets.randbits(64)
            return sid if sid != 0 else 1
        return 0x1122334455667788

    value = value.strip().lower()

    if value in {"random", "rand", "auto"}:
        sid = secrets.randbits(64)
        return sid if sid != 0 else 1

    return int(value, 0)


@dataclass(frozen=True)
class Config:
    enable: bool = _bool("SECURE_PATCH_ENABLE", False)
    debug: bool = _bool("SECURE_PATCH_DEBUG", False)
    strict: bool = _bool("SECURE_PATCH_STRICT", True)
    kms_socket: str = _str("SECURE_PATCH_KMS_SOCKET", "/run/kmsagent/socket/kmsagent.sock")
    session_id: int = _session_id("SECURE_PATCH_SESSION_ID", default_random=True)
    kms_timeout: float = _float("SECURE_PATCH_KMS_TIMEOUT", 10.0)
    device_id_mode: str = _str("SECURE_PATCH_DEVICE_ID_MODE", "A2")
    kms_retry_max: int = _int("SECURE_PATCH_KMS_RETRY_MAX", 3)
    kms_retry_wait_ms: int = _int("SECURE_PATCH_KMS_RETRY_WAIT_MS", 50)
    kms_retry_backoff: float = _float("SECURE_PATCH_KMS_RETRY_BACKOFF", 1.0)
    kms_connect_timeout_ms: int = _int("SECURE_PATCH_KMS_CONNECT_TIMEOUT_MS", 200)
    kms_recv_timeout_ms: int = _int("SECURE_PATCH_KMS_RECV_TIMEOUT_MS", 500)
    kms_async_enable: bool = _bool("SECURE_PATCH_KMS_ASYNC_ENABLE", False)
    kms_async_queue_size: int = _int("SECURE_PATCH_KMS_ASYNC_QUEUE_SIZE", 128)
    alg_id: int = _int("SECURE_PATCH_ALG_ID", ALG_AES_CTR_128)
    iv_bytes: int = _int("SECURE_PATCH_IV_BYTES", 16)
    rotate_bytes: int = _int("SECURE_PATCH_ROTATE_BYTES", 1073741824)
    rotate_ops: int = _int("SECURE_PATCH_ROTATE_OPS", 100000)
    rotate_prefetch_ratio: float = _float("SECURE_PATCH_ROTATE_PREFETCH_RATIO", 0.8)
    rotate_allow_stale: bool = _bool("SECURE_PATCH_ROTATE_ALLOW_STALE", True)
    max_keys_per_direction: int = _int("SECURE_PATCH_MAX_KEYS_PER_DIRECTION", 2)
    fallback_enable: bool = _bool("SECURE_PATCH_KEY_FALLBACK_ENABLE", True)
    failure_demote_threshold: int = _int("SECURE_PATCH_KEY_FAILURE_DEMOTE_THRESHOLD", 3)
    verify_roundtrip: bool = _bool("SECURE_PATCH_VERIFY_ROUNDTRIP", False)
    patch_copy_to_gpu: bool = _bool("SECURE_PATCH_PATCH_VLLM_COPY_TO_GPU", True)
    patch_async_output_d2h: bool = _bool("SECURE_PATCH_PATCH_ASYNC_OUTPUT_D2H", True)
    patch_weight_loader: bool = _bool("SECURE_PATCH_PATCH_WEIGHT_LOADER", False)
    host_ctr_module: str = _str("SECURE_PATCH_HOST_CTR_MODULE", "aes_ctr_crypt")
    host_ctr_function: str = _str("SECURE_PATCH_HOST_CTR_FUNCTION", "aes_ctr_cryption")
    host_gcm_module: str = _str("SECURE_PATCH_HOST_GCM_MODULE", "aes_gcm_crypt")
    host_gcm_function: str = _str("SECURE_PATCH_HOST_GCM_FUNCTION", "aes_gcm_cryption")


CONFIG = Config()
