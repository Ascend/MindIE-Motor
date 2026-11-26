# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Response envelopes preserve the current JSON and HTTP error contracts."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import HTTPException

from motor.common.http import http_response


@pytest.mark.parametrize("data", [None, {}, [], "", 0, False, {"items": [1, "中文", None]}, [1, 2]])
def test_success_envelope_normalizes_falsy_data_before_json_round_trip(data: Any) -> None:
    """The current contract replaces falsy data with an empty object before serialization."""
    response = http_response.format_success_response(data, message="完成")
    assert json.loads(json.dumps(response)) == {"code": 200, "message": "完成", "data": data or {}}


def test_default_success_responses_do_not_share_mutable_data() -> None:
    first = http_response.format_success_response()
    first["data"]["changed"] = True
    assert http_response.format_success_response() == {"code": 200, "message": "Success", "data": {}}


@pytest.mark.parametrize("code,data", [(None, None), (0, []), (9001, {"field": "模型"})])
def test_exception_keeps_http_status_separate_from_business_code(code: int | None, data: Any) -> None:
    with pytest.raises(HTTPException) as error:
        http_response.raise_http_exception("invalid", status_code=422, code=code, data=data)
    assert error.value.status_code == 422
    assert json.loads(json.dumps(error.value.detail)) == {
        "code": code or 422,
        "message": "invalid",
        "data": data or {},
    }


@pytest.mark.parametrize(
    "raiser,status",
    [
        (http_response.raise_bad_request, 400),
        (http_response.raise_unauthorized, 401),
        (http_response.raise_forbidden, 403),
        (http_response.raise_not_found, 404),
        (http_response.raise_internal_error, 500),
        (http_response.raise_http_exception, 500),
    ],
)
def test_error_helpers_preserve_default_and_custom_details(raiser: Callable, status: int) -> None:
    with pytest.raises(HTTPException) as default:
        raiser("failed")
    assert default.value.status_code == status
    assert default.value.detail == {"code": status, "message": "failed", "data": {}}
    with pytest.raises(HTTPException) as custom:
        raiser("failed", code=9100, data={"retry": False})
    assert custom.value.status_code == status
    assert custom.value.detail == {"code": 9100, "message": "failed", "data": {"retry": False}}
