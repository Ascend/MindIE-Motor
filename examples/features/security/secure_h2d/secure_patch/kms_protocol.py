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

import struct
from dataclasses import dataclass
from typing import List

from .constants import (
    OP_KEY_REQ,
    OP_KEY_UPDATE_REQ,
    RSP_KEY_REQ,
    RSP_KEY_UPDATE_REQ,
    VERSION,
)

REQ_STRUCT = struct.Struct("<IIIQIIII")
RSP_HDR_STRUCT = struct.Struct("<IIIiQII")
KEY_REC_HDR_STRUCT = struct.Struct("<IIII")


@dataclass(frozen=True)
class KeyContext:
    alg_id: int
    key_id: int
    key_type: int
    key_len: int
    key_bytes: bytes


@dataclass(frozen=True)
class KmsResponse:
    version: int
    rsp_opcode: int
    rsp_body_len: int
    retcode: int
    session_id: int
    device_id: int
    count: int
    records: List[KeyContext]


def build_key_request_body(
    *, opcode: int, session_id: int, device_id: int, alg_id: int, count: int = 0, key_id: int = 0
) -> bytes:
    return REQ_STRUCT.pack(
        VERSION,
        opcode,
        REQ_STRUCT.size,
        int(session_id),
        int(device_id),
        int(alg_id),
        int(count),
        int(key_id),
    )


def parse_kms_response(body: bytes, *, expect_rsp_opcode: int) -> KmsResponse:
    if len(body) < RSP_HDR_STRUCT.size:
        raise RuntimeError(f"KMS response too short: got={len(body)}")

    version, rsp_opcode, rsp_body_len, retcode, session_id, device_id, count = RSP_HDR_STRUCT.unpack_from(body, 0)

    if rsp_opcode != expect_rsp_opcode:
        raise RuntimeError(f"unexpected rsp_opcode={rsp_opcode}, expect={expect_rsp_opcode}")
    if retcode != 0:
        raise RuntimeError(f"KMS retcode={retcode}")

    offset = RSP_HDR_STRUCT.size
    records: List[KeyContext] = []
    for idx in range(count):
        if offset + KEY_REC_HDR_STRUCT.size > len(body):
            raise RuntimeError(f"KMS response truncated at key header idx={idx}")
        alg_id, key_id, key_type, key_len = KEY_REC_HDR_STRUCT.unpack_from(body, offset)
        offset += KEY_REC_HDR_STRUCT.size
        if key_len <= 0:
            raise RuntimeError(f"invalid key_len={key_len} at idx={idx}")
        if offset + key_len > len(body):
            raise RuntimeError(f"KMS response truncated at key bytes idx={idx}")
        key_bytes = body[offset : offset + key_len]
        offset += key_len
        records.append(KeyContext(alg_id, key_id, key_type, key_len, key_bytes))

    return KmsResponse(version, rsp_opcode, rsp_body_len, retcode, session_id, device_id, count, records)


def request_opcode_and_expected(update: bool) -> tuple[int, int]:
    if update:
        return OP_KEY_UPDATE_REQ, RSP_KEY_UPDATE_REQ
    return OP_KEY_REQ, RSP_KEY_REQ
