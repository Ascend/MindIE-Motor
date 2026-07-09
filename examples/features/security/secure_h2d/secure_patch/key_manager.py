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

import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import torch

from .config import CONFIG
from .constants import KEY_TYPE_D2H, KEY_TYPE_H2D
from .device_mapping import (
    global_device_id_from_local,
    kms_device_id_from_global,
)
from .kms_client import KmsClient
from .kms_protocol import KeyContext
from .runtime import log


RingKey = Tuple[int, int, int]  # kms_device_id, alg_id, key_type
LatestKey = Tuple[int, int, int]  # kms_device_id, alg_id, key_type
UsageKey = Tuple[int, int]  # kms_device_id, alg_id
FailureKey = Tuple[int, int, int, int]  # kms_device_id, alg_id, key_type, key_id
KeyAddrCacheKey = Tuple[int, int, int, int]  # local_device_id, alg_id, key_type, key_id
InitialRequestKey = Tuple[int, int]  # kms_device_id, alg_id


@dataclass
class UsageState:
    ops: int = 0
    bytes: int = 0
    update_inflight: bool = False


class KeyManager:
    def __init__(self) -> None:
        self._client = KmsClient()
        self._lock = threading.RLock()

        self._rings: Dict[RingKey, List[KeyContext]] = {}
        self._latest: Dict[LatestKey, KeyContext] = {}
        self._usage: Dict[UsageKey, UsageState] = {}
        self._failures: Dict[FailureKey, int] = {}

        self._key_addr: Dict[KeyAddrCacheKey, torch.Tensor] = {}
        self._key_addr_locks: Dict[KeyAddrCacheKey, threading.Lock] = {}

        self._iv_counter: Dict[KeyAddrCacheKey, int] = {}

        self._initial_request_inflight: Dict[InitialRequestKey, threading.Event] = {}

    def _ids_from_local(self, local_device_id: int) -> tuple[int, int]:
        global_device_id = global_device_id_from_local(local_device_id)
        kms_device_id = kms_device_id_from_global(global_device_id)
        return global_device_id, kms_device_id

    def _iter_response_keys(self, rsp) -> Iterable[KeyContext]:
        for attr in ("keys", "records", "key_records"):
            value = getattr(rsp, attr, None)
            if value is not None:
                return value
        raise RuntimeError(f"KMS response has no keys/records/key_records: {rsp!r}")

    def _response_device_id(self, rsp) -> int:
        for attr in ("device_id", "device", "kms_device_id"):
            if hasattr(rsp, attr):
                return int(getattr(rsp, attr))
        raise RuntimeError(f"KMS response has no device_id: {rsp!r}")

    def _max_keys_per_side(self) -> int:
        return int(getattr(CONFIG, "max_keys_per_side", 2))

    def _store_response_locked(self, rsp) -> None:
        kms_device_id = self._response_device_id(rsp)

        for key in self._iter_response_keys(rsp):
            ring_key: RingKey = (
                int(kms_device_id),
                int(key.alg_id),
                int(key.key_type),
            )
            latest_key: LatestKey = ring_key

            ring = self._rings.setdefault(ring_key, [])
            ring = [k for k in ring if int(k.key_id) != int(key.key_id)]
            ring.insert(0, key)

            max_keys = self._max_keys_per_side()
            evicted = ring[max_keys:]
            ring = ring[:max_keys]
            self._rings[ring_key] = ring
            self._latest[latest_key] = key

            for old in evicted:
                self._evict_key_addr_for_key_locked(old)

    def _evict_key_addr_for_key_locked(self, key: KeyContext) -> None:
        for ck in list(self._key_addr.keys()):
            _, ck_alg, ck_type, ck_id = ck
            if int(ck_alg) == int(key.alg_id) and int(ck_type) == int(key.key_type) and int(ck_id) == int(key.key_id):
                self._key_addr.pop(ck, None)

        for ck in list(self._key_addr_locks.keys()):
            _, ck_alg, ck_type, ck_id = ck
            if int(ck_alg) == int(key.alg_id) and int(ck_type) == int(key.key_type) and int(ck_id) == int(key.key_id):
                lock = self._key_addr_locks.get(ck)
                if lock is not None and not lock.locked():
                    self._key_addr_locks.pop(ck, None)

    def _has_keys_locked(self, *, kms_device_id: int, alg_id: int) -> bool:
        h2d = self._latest.get((int(kms_device_id), int(alg_id), KEY_TYPE_H2D))
        d2h = self._latest.get((int(kms_device_id), int(alg_id), KEY_TYPE_D2H))
        return h2d is not None and d2h is not None

    def ensure_keys(self, *, device_id: int, alg_id: int) -> None:
        local_device_id = int(device_id)
        global_device_id, kms_device_id = self._ids_from_local(local_device_id)
        request_key: InitialRequestKey = (int(kms_device_id), int(alg_id))

        while True:
            owner = False

            with self._lock:
                if self._has_keys_locked(kms_device_id=kms_device_id, alg_id=alg_id):
                    return

                event = self._initial_request_inflight.get(request_key)
                if event is None:
                    event = threading.Event()
                    self._initial_request_inflight[request_key] = event
                    owner = True

            if owner:
                break
            event.wait()

        try:
            rsp = self._client.request_keys(
                device_id=kms_device_id,
                alg_id=alg_id,
            )

            with self._lock:
                self._store_response_locked(rsp)

        finally:
            with self._lock:
                inflight = self._initial_request_inflight.pop(request_key, None)
                if inflight is not None:
                    inflight.set()

    def get_candidate_keys(self, *, device_id: int, alg_id: int, key_type: int) -> list[KeyContext]:
        local_device_id = int(device_id)
        _, kms_device_id = self._ids_from_local(local_device_id)

        self.ensure_keys(device_id=local_device_id, alg_id=alg_id)

        with self._lock:
            latest = self._latest.get((kms_device_id, int(alg_id), int(key_type)))
            ring = list(self._rings.get((kms_device_id, int(alg_id), int(key_type)), []))

        if latest is None:
            return ring

        result = [latest]
        for key in ring:
            if int(key.key_id) != int(latest.key_id):
                result.append(key)
        return result

    def _key_addr_key(self, *, local_device_id: int, key: KeyContext) -> KeyAddrCacheKey:
        return (
            int(local_device_id),
            int(key.alg_id),
            int(key.key_type),
            int(key.key_id),
        )

    def get_key_addr(self, *, device_id: int, key: KeyContext) -> Optional[torch.Tensor]:
        local_device_id = int(device_id)
        ck = self._key_addr_key(local_device_id=local_device_id, key=key)
        with self._lock:
            return self._key_addr.get(ck)

    def put_key_addr(self, *, device_id: int, key: KeyContext, key_addr_tensor: torch.Tensor) -> None:
        local_device_id = int(device_id)
        ck = self._key_addr_key(local_device_id=local_device_id, key=key)
        with self._lock:
            self._key_addr[ck] = key_addr_tensor

    def get_key_addr_lock(self, *, device_id: int, key: KeyContext) -> threading.Lock:
        local_device_id = int(device_id)
        ck = self._key_addr_key(local_device_id=local_device_id, key=key)
        with self._lock:
            lock = self._key_addr_locks.get(ck)
            if lock is None:
                lock = threading.Lock()
                self._key_addr_locks[ck] = lock
            return lock

    def next_iv(self, *, device_id: int, key: KeyContext, iv_bytes: int) -> bytes:
        local_device_id = int(device_id)
        counter_key = (
            local_device_id,
            int(key.alg_id),
            int(key.key_type),
            int(key.key_id),
        )

        with self._lock:
            counter = int(self._iv_counter.get(counter_key, 0))
            self._iv_counter[counter_key] = counter + 1

        return counter.to_bytes(int(iv_bytes), byteorder="big", signed=False)

    def _usage_state_locked(self, *, kms_device_id: int, alg_id: int) -> UsageState:
        key: UsageKey = (int(kms_device_id), int(alg_id))
        state = self._usage.get(key)
        if state is None:
            state = UsageState()
            self._usage[key] = state
        return state

    def record_success(self, *, device_id: int, key: KeyContext, num_bytes: int) -> None:
        local_device_id = int(device_id)
        global_device_id, kms_device_id = self._ids_from_local(local_device_id)

        with self._lock:
            state = self._usage_state_locked(kms_device_id=kms_device_id, alg_id=key.alg_id)
            state.ops += 1
            state.bytes += int(num_bytes)

            rotate_ops = int(getattr(CONFIG, "rotate_ops", 0))
            rotate_bytes = int(getattr(CONFIG, "rotate_bytes", 0))

            need_update = False
            if 0 < rotate_ops <= state.ops:
                need_update = True
            if 0 < rotate_bytes <= state.bytes:
                need_update = True

            if not need_update or state.update_inflight:
                return

            state.update_inflight = True

        try:
            self._update_keys(
                local_device_id=local_device_id,
                global_device_id=global_device_id,
                kms_device_id=kms_device_id,
                alg_id=int(key.alg_id),
            )
        finally:
            with self._lock:
                state = self._usage_state_locked(kms_device_id=kms_device_id, alg_id=key.alg_id)
                state.ops = 0
                state.bytes = 0
                state.update_inflight = False

    def _update_keys(
        self,
        *,
        local_device_id: int,
        global_device_id: int,
        kms_device_id: int,
        alg_id: int,
    ) -> None:
        def _latest_id(key_type: int):
            key = self._latest.get((int(kms_device_id), int(alg_id), int(key_type)))
            return None if key is None else int(key.key_id)

        if not hasattr(self._client, "update_keys"):
            raise RuntimeError("KmsClient has no update_keys method")

        with self._lock:
            old_h2d_id = _latest_id(KEY_TYPE_H2D)
            old_d2h_id = _latest_id(KEY_TYPE_D2H)

        rsp = self._client.update_keys(
            device_id=kms_device_id,
            alg_id=alg_id,
        )

        with self._lock:
            self._store_response_locked(rsp)

            new_h2d_id = _latest_id(KEY_TYPE_H2D)
            new_d2h_id = _latest_id(KEY_TYPE_D2H)

        log(
            "KMS key updated: "
            f"local_device={local_device_id}, "
            f"global_device={global_device_id}, "
            f"kms_device={kms_device_id}, "
            f"alg={alg_id}, "
            f"H2D={old_h2d_id}->{new_h2d_id}, "
            f"D2H={old_d2h_id}->{new_d2h_id}"
        )

    def record_failure(self, *, device_id: int, key: KeyContext, error: Exception) -> None:
        local_device_id = int(device_id)
        _, kms_device_id = self._ids_from_local(local_device_id)

        fk: FailureKey = (
            int(kms_device_id),
            int(key.alg_id),
            int(key.key_type),
            int(key.key_id),
        )

        with self._lock:
            count = self._failures.get(fk, 0) + 1
            self._failures[fk] = count


_MANAGER: Optional[KeyManager] = None
_MANAGER_LOCK = threading.Lock()


def get_key_manager() -> KeyManager:
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = KeyManager()
    return _MANAGER
