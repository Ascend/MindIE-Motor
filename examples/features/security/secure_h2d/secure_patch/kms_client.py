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

import socket
import struct
import time
from typing import Optional

from .config import CONFIG
from .kms_protocol import build_key_request_body, parse_kms_response, request_opcode_and_expected
from .runtime import log


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    left = n
    while left > 0:
        chunk = sock.recv(left)
        if not chunk:
            raise RuntimeError(f"socket closed while receiving {n} bytes")
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


class KmsClient:
    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float | None = None,
        session_id: int | None = None,
    ):
        self.socket_path = CONFIG.kms_socket if socket_path is None else socket_path
        self.timeout = CONFIG.kms_timeout if timeout is None else timeout
        self.session_id = CONFIG.session_id if session_id is None else session_id

    def _connect_timeout(self) -> float:
        value = getattr(CONFIG, "kms_connect_timeout_ms", 0)
        if value and value > 0:
            return max(float(value) / 1000.0, 0.001)
        return float(self.timeout)

    def _recv_timeout(self) -> float:
        value = getattr(CONFIG, "kms_recv_timeout_ms", 0)
        if value and value > 0:
            return max(float(value) / 1000.0, 0.001)
        return float(self.timeout)

    def _send_once(self, body: bytes) -> bytes:
        prefix = struct.pack("=I", socket.htonl(len(body)))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._connect_timeout())
            sock.connect(self.socket_path)

            sock.settimeout(self._recv_timeout())
            sock.sendall(prefix)
            sock.sendall(body)

            rsp_prefix = _recv_exact(sock, 4)
            rsp_len = socket.ntohl(struct.unpack("=I", rsp_prefix)[0])

            if rsp_len <= 0:
                raise RuntimeError(f"invalid KMS response length={rsp_len}")

            return _recv_exact(sock, rsp_len)

    def request_keys(self, *, device_id: int, alg_id: int):
        return self._request(update=False, device_id=device_id, alg_id=alg_id)

    def update_keys(self, *, device_id: int, alg_id: int):
        return self._request(update=True, device_id=device_id, alg_id=alg_id)

    def _request_once(self, *, update: bool, device_id: int, alg_id: int):
        opcode, expect = request_opcode_and_expected(update)

        body = build_key_request_body(
            opcode=opcode,
            session_id=self.session_id,
            device_id=device_id,
            alg_id=alg_id,
            count=0,
            key_id=0,
        )
        rsp_body = self._send_once(body)
        rsp = parse_kms_response(rsp_body, expect_rsp_opcode=expect)
        return rsp

    def _request(self, *, update: bool, device_id: int, alg_id: int):
        opcode, _ = request_opcode_and_expected(update)

        retry_max = max(int(getattr(CONFIG, "kms_retry_max", 0)), 0)
        wait_ms = max(int(getattr(CONFIG, "kms_retry_wait_ms", 0)), 0)
        backoff = float(getattr(CONFIG, "kms_retry_backoff", 1.0))
        if backoff <= 0:
            backoff = 1.0

        total_attempts = retry_max + 1
        last_error: Optional[BaseException] = None

        for attempt in range(total_attempts):
            try:
                rsp = self._request_once(update=update, device_id=device_id, alg_id=alg_id)

                if attempt > 0:
                    log(
                        f"KMS request recovered: opcode={opcode}, "
                        f"device={device_id}, alg={alg_id}, "
                        f"attempt={attempt + 1}/{total_attempts}"
                    )

                return rsp

            except BaseException as exc:
                last_error = exc

                if attempt >= retry_max:
                    break

                sleep_ms = wait_ms * (backoff**attempt)

                log(
                    f"KMS request failed, retrying: opcode={opcode}, "
                    f"device={device_id}, alg={alg_id}, "
                    f"attempt={attempt + 1}/{total_attempts}, "
                    f"sleep_ms={sleep_ms:.1f}, error={exc!r}"
                )

                if sleep_ms > 0:
                    time.sleep(sleep_ms / 1000.0)

        raise RuntimeError(
            f"KMS request failed after retries: opcode={opcode}, "
            f"device={device_id}, alg={alg_id}, "
            f"attempts={total_attempts}, last_error={last_error!r}"
        )
