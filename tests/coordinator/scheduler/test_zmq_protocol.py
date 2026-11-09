# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import sys
from pathlib import Path
from enum import Enum
import pytest
import msgspec

# 动态获取项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

# 导入真实的源码模块 (加上 noqa 避免 ruff 报 E402)
from motor.coordinator.scheduler.runtime.zmq_protocol import (  # noqa: E402
    ZMQMessageSerializer,
    SchedulerRequest,
    SchedulerResponse,
    ZERO_COPY_REF_KEY,
    _enc_hook,
    _replace_large_with_refs,
    _resolve_refs,
    pack_send_frames,
    unpack_recv_payload,
)


class DummyEnum(Enum):
    TEST_VAL = "test_value"


class MockPydanticModel:
    def model_dump(self, mode="json"):
        return {"model_key": "model_val"}


def test_enc_hook():
    # 测试 Enum 编码
    assert _enc_hook(DummyEnum.TEST_VAL) == "test_value"
    # 测试 Pydantic 兼容编码
    assert _enc_hook(MockPydanticModel()) == {"model_key": "model_val"}
    # 测试不支持的类型抛异常
    with pytest.raises(TypeError, match="not supported"):
        _enc_hook(object())


def test_serializer_single_frame_roundtrip():
    """对应审核意见：所有传入的 data 字段都必须断言，防止丢字段。"""
    serializer = ZMQMessageSerializer(zero_copy_threshold=1024)

    original_data = {
        "msg": "hello",
        "enum_val": DummyEnum.TEST_VAL,
        "candidate_policy": "load_balance",
        "nested": {"a": 1, "b": 2},
    }

    req = SchedulerRequest(
        request_type="test_req",
        request_id="123",
        data=original_data,
    )

    # 序列化
    encoded = serializer.serialize_request(req)
    assert isinstance(encoded, bytes)

    # 反序列化
    decoded = serializer.deserialize_request(encoded)

    # 强断言：必须和原始数据完全一致，不能少任何 key
    assert decoded.request_type == "test_req"
    assert decoded.request_id == "123"
    assert decoded.data["msg"] == "hello"
    assert decoded.data["enum_val"] == "test_value"
    assert decoded.data["candidate_policy"] == "load_balance"
    assert decoded.data["nested"] == {"a": 1, "b": 2}
    assert len(decoded.data) == len(original_data)  # 确保没有丢字段


def test_serializer_zero_copy_roundtrip():
    """测试强制触发多帧 zero-copy 后，数据依然不丢失。"""
    serializer = ZMQMessageSerializer(zero_copy_threshold=10)

    large_data = {"large_key": "a" * 20}

    # Request 测试
    req = SchedulerRequest(
        request_type="test",
        request_id="1",
        data={"nested": large_data, "candidate_policy": "kv_cache_affinity"},
    )
    encoded_req = serializer.serialize_request(req)
    assert isinstance(encoded_req, list)
    assert len(encoded_req) == 2

    decoded_req = serializer.deserialize_request(encoded_req)
    assert decoded_req.data["nested"]["large_key"] == "a" * 20
    assert decoded_req.data["candidate_policy"] == "kv_cache_affinity"

    # Response 测试
    large_list = ["a" * 20]
    res = SchedulerResponse(response_type="success", request_id="1", data={"items": large_list})
    encoded_res = serializer.serialize_response(res)
    assert isinstance(encoded_res, list)

    decoded_res = serializer.deserialize_response(encoded_res)
    assert decoded_res.data["items"][0] == "a" * 20


def test_serializer_invalid_bytes():
    """对应审核意见：使用具体异常类型，禁止使用宽泛的 Exception。"""
    serializer = ZMQMessageSerializer()

    # 1. 非法字节流
    with pytest.raises(msgspec.DecodeError):
        serializer.deserialize_request(b"not_a_msgpack_stream")

    # 2. 截断的合法字节流
    valid_req = SchedulerRequest(request_type="test", request_id="1", data={})
    valid_bytes = serializer.serialize_request(valid_req)
    truncated = valid_bytes[:5]
    with pytest.raises(msgspec.DecodeError):
        serializer.deserialize_request(truncated)

    # 3. 错误的类型结构
    wrong_type_bytes = msgspec.msgpack.encode(12345)
    with pytest.raises(msgspec.ValidationError):
        serializer.deserialize_request(wrong_type_bytes)


def test_replace_and_resolve_refs_internal():
    """测试底层的引用替换和解析逻辑（对应正常路径）。"""
    encoder = msgspec.msgpack.Encoder(enc_hook=_enc_hook)
    decoder = msgspec.msgpack.Decoder()

    # 测试正常解析
    aux_list = [encoder.encode({"real_data": 123})]
    ref_dict = {ZERO_COPY_REF_KEY: 0}
    resolved = _resolve_refs(ref_dict, aux_list, decoder)
    assert resolved == {"real_data": 123}

    # 测试已经是引用格式的 dict 不会被重复替换
    out = _replace_large_with_refs(ref_dict, 10, [], encoder)
    assert out == ref_dict


@pytest.mark.xfail(reason="Known bug: missing aux frames silently pass through instead of raising")
def test_deserialize_request_missing_aux_fails():
    """对应检视意见：辅帧缺失时，预期应报错，当前代码存在静默放行的缺陷。"""
    serializer = ZMQMessageSerializer(zero_copy_threshold=10)
    req = SchedulerRequest(request_type="test", request_id="1", data={"nested": {"large_key": "a" * 20}})
    encoded = serializer.serialize_request(req)
    assert isinstance(encoded, list)

    # 模拟丢掉辅帧，只传主帧
    with pytest.raises(msgspec.DecodeError):
        serializer.deserialize_request(encoded[:1])


@pytest.mark.xfail(reason="Known bug: invalid ref index silently pass through instead of raising")
def test_deserialize_request_invalid_index_fails():
    """对应检视意见：非法越界索引时，预期应报错，当前代码存在静默放行的缺陷。"""
    serializer = ZMQMessageSerializer()
    invalid_data = {"a": {ZERO_COPY_REF_KEY: 999}}
    main_buf = msgspec.msgpack.encode(SchedulerRequest(request_type="test", request_id="1", data=invalid_data))

    # 传空辅帧触发越界
    with pytest.raises(msgspec.DecodeError):
        serializer.deserialize_request([main_buf])


def test_pack_and_unpack_frames():
    prefix = [b"routing_id", b""]

    packed_single = pack_send_frames(prefix, b"data")
    assert packed_single == prefix + [b"data"]

    multi_payload = [b"main", b"aux1", b"aux2"]
    packed_multi = pack_send_frames(prefix, multi_payload)
    assert packed_multi == prefix + multi_payload

    assert unpack_recv_payload([b"routing_id"], payload_start=1) == b""
    assert unpack_recv_payload([b"routing_id", b"main"], payload_start=1) == b"main"
    assert unpack_recv_payload([b"routing_id", b"main", b"aux1"], payload_start=1) == [
        b"main",
        b"aux1",
    ]
