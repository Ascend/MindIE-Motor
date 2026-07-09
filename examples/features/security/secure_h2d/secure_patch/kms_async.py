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

import atexit
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Set, Tuple

from .config import CONFIG
from .kms_client import KmsClient
from .kms_protocol import KmsResponse
from .runtime import log

OP_REQUEST = 1
OP_UPDATE = 3


@dataclass(frozen=True)
class KmsAsyncTask:
    opcode: int
    device_id: int
    alg_id: int
    reason: str = ""


ApplyResponseFn = Callable[[KmsResponse, str], None]


class KmsAsyncWorker:
    def __init__(self, apply_response: ApplyResponseFn) -> None:
        self._apply_response = apply_response
        self._queue: "queue.Queue[KmsAsyncTask]" = queue.Queue(maxsize=max(int(CONFIG.kms_async_queue_size), 1))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._inflight: Set[Tuple[int, int, int]] = set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._thread = threading.Thread(
                target=self._run,
                name="secure_patch_kms_async_worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def submit(self, task: KmsAsyncTask) -> bool:
        if not CONFIG.kms_async_enable:
            return False

        key = (int(task.opcode), int(task.device_id), int(task.alg_id))

        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)

        try:
            self._queue.put_nowait(task)
            return True

        except queue.Full:
            with self._lock:
                self._inflight.discard(key)
            return False

    def _run(self) -> None:
        client = KmsClient()

        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            inflight_key = (int(task.opcode), int(task.device_id), int(task.alg_id))

            try:
                if task.opcode == OP_REQUEST:
                    rsp = client.request_keys(device_id=task.device_id, alg_id=task.alg_id)
                elif task.opcode == OP_UPDATE:
                    rsp = client.update_keys(device_id=task.device_id, alg_id=task.alg_id)
                else:
                    raise RuntimeError(f"unsupported KMS async opcode: {task.opcode}")

                self._apply_response(rsp, task.reason or "kms_async")

            except BaseException as exc:
                log(
                    f"KMS async task failed: opcode={task.opcode}, "
                    f"device={task.device_id}, alg={task.alg_id}, "
                    f"reason={task.reason}, error={exc!r}"
                )

            finally:
                with self._lock:
                    self._inflight.discard(inflight_key)
                self._queue.task_done()


_WORKER_LOCK = threading.Lock()
_WORKER: Optional[KmsAsyncWorker] = None


def _get_worker(apply_response: ApplyResponseFn) -> KmsAsyncWorker:
    global _WORKER

    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = KmsAsyncWorker(apply_response)
            _WORKER.start()
            atexit.register(_WORKER.stop)

        return _WORKER


def submit_async_request(
    *,
    device_id: int,
    alg_id: int,
    apply_response: ApplyResponseFn,
    reason: str,
) -> bool:
    if not CONFIG.kms_async_enable:
        return False

    worker = _get_worker(apply_response)
    return worker.submit(
        KmsAsyncTask(
            opcode=OP_REQUEST,
            device_id=device_id,
            alg_id=alg_id,
            reason=reason,
        )
    )


def submit_async_update(
    *,
    device_id: int,
    alg_id: int,
    apply_response: ApplyResponseFn,
    reason: str,
) -> bool:
    if not CONFIG.kms_async_enable:
        return False

    worker = _get_worker(apply_response)
    return worker.submit(
        KmsAsyncTask(
            opcode=OP_UPDATE,
            device_id=device_id,
            alg_id=alg_id,
            reason=reason,
        )
    )
