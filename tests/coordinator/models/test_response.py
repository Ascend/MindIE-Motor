# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for the coordinator API response models."""

import pytest
from pydantic import ValidationError

from motor.coordinator.models.response import ErrorResponse, RequestResponse


def allows_null(prop: dict) -> bool:
    return any(sub.get("type") == "null" for sub in prop.get("anyOf", []))


# ① RequestResponse：全字段正常构造
def test_request_response_constructs_with_all_fields():
    response = RequestResponse(
        request_id="req-001", status="success", message="done", data={"task_id": "t1", "count": 3}
    )
    assert response.request_id == "req-001"
    assert response.status == "success"
    assert response.message == "done"
    assert response.data == {"task_id": "t1", "count": 3}


# ① RequestResponse：message/data 默认值为 None
def test_request_response_defaults_message_and_data_to_none():
    response = RequestResponse(request_id="req-002", status="pending")
    assert response.message is None
    assert response.data is None
    assert response.model_dump() == {
        "request_id": "req-002",
        "status": "pending",
        "message": None,
        "data": None,
    }


# ① RequestResponse：JSON 序列化/反序列化往返保持等价
@pytest.mark.parametrize(
    "data",
    [
        {"single": "value"},
        {"nested": {"items": [1, 2, 3], "flag": True, "nothing": None}},
        {"中文键": "中文值"},
    ],
)
def test_request_response_json_round_trip_preserves_payload(data):
    original = RequestResponse(request_id="req-003", status="success", message="ok", data=data)
    restored = RequestResponse.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.data == data


# ① RequestResponse：必填字段缺失触发 ValidationError
@pytest.mark.parametrize("missing_field", ["request_id", "status"])
def test_request_response_missing_required_field_raises_validation_error(missing_field):
    payload = {"request_id": "req-004", "status": "ok"}
    payload.pop(missing_field)
    with pytest.raises(ValidationError) as exc_info:
        RequestResponse(**payload)
    assert exc_info.value.errors()[0]["loc"] == (missing_field,)
    assert exc_info.value.errors()[0]["type"] == "missing"


# ② ErrorResponse：全字段正常构造与序列化格式
def test_error_response_constructs_with_all_fields_and_serializes():
    error = ErrorResponse(message="not found", type="NotFound", code=404)
    assert error.message == "not found"
    assert error.type == "NotFound"
    assert error.code == 404
    assert error.model_dump_json() == '{"message":"not found","type":"NotFound","code":404}'


# ② ErrorResponse：任一必填字段缺失触发 ValidationError
@pytest.mark.parametrize("missing_field", ["message", "type", "code"])
def test_error_response_missing_required_field_raises_validation_error(missing_field):
    payload = {"message": "internal error", "type": "Internal", "code": 500}
    payload.pop(missing_field)
    with pytest.raises(ValidationError) as exc_info:
        ErrorResponse(**payload)
    assert exc_info.value.errors()[0]["loc"] == (missing_field,)
    assert exc_info.value.errors()[0]["type"] == "missing"


# ③ JSON Schema：RequestResponse 字段、必填项与可空性
def test_request_response_json_schema_describes_fields():
    schema = RequestResponse.model_json_schema()
    assert schema["title"] == "RequestResponse"
    assert set(schema["required"]) == {"request_id", "status"}
    properties = schema["properties"]
    assert set(properties) == {"request_id", "status", "message", "data"}
    assert properties["request_id"]["type"] == "string"
    assert properties["status"]["type"] == "string"
    assert allows_null(properties["message"])
    assert allows_null(properties["data"])


# ③ JSON Schema：ErrorResponse 字段、必填项与类型
def test_error_response_json_schema_describes_fields():
    schema = ErrorResponse.model_json_schema()
    assert schema["title"] == "ErrorResponse"
    assert set(schema["required"]) == {"message", "type", "code"}
    properties = schema["properties"]
    assert properties["message"]["type"] == "string"
    assert properties["type"]["type"] == "string"
    assert properties["code"]["type"] == "integer"
