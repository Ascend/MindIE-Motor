# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

from motor.engine_server.core.serving_error import map_serving_exception


class BadRequestError(Exception):
    pass


class _StatusError(Exception):
    def __init__(self, status_code, detail, headers=None):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


class _ResponseError(Exception):
    def __init__(self, status_code, payload, headers=None):
        super().__init__("upstream rejected request")
        self.response = SimpleNamespace(
            status_code=status_code,
            json=lambda: payload,
            text="",
            headers=headers or {},
        )


class _AsyncJSONResponse:
    status_code = 422
    text = "async json fallback"
    headers = {}

    async def json(self):
        return {"error": "must not become detail"}


class _AsyncResponseError(Exception):
    def __init__(self):
        super().__init__("async response error")
        self.response = _AsyncJSONResponse()


class _NonJSONResponse:
    status_code = 400
    text = "plain fallback"
    headers = {}

    @staticmethod
    def json():
        return {"invalid": object()}


class _NonJSONResponseError(Exception):
    def __init__(self):
        super().__init__("non-json response error")
        self.response = _NonJSONResponse()


class _NonFiniteJSONResponse:
    status_code = 400
    text = "non-finite fallback"
    headers = {}

    @staticmethod
    def json():
        return {"value": float("nan")}


class _NonFiniteJSONResponseError(Exception):
    def __init__(self):
        super().__init__("non-finite response error")
        self.response = _NonFiniteJSONResponse()


def test_map_serving_exception_preserves_http_exception():
    original = HTTPException(
        status_code=429,
        detail="rate limited",
        headers={"Retry-After": "3"},
    )

    assert map_serving_exception(original) is original
    assert original.headers == {"Retry-After": "3"}


def test_map_serving_exception_preserves_starlette_http_exception_semantics():
    original = StarletteHTTPException(status_code=404, detail="model not found")
    mapped = map_serving_exception(original)

    assert isinstance(mapped, HTTPException)
    assert mapped.status_code == 404
    assert mapped.detail == "model not found"


def test_map_serving_exception_preserves_custom_status_and_safe_headers():
    mapped = map_serving_exception(
        _StatusError(
            503,
            {"error": {"message": "engine unavailable"}},
            headers={"Retry-After": "2", "Content-Length": "999"},
        )
    )

    assert mapped.status_code == 503
    assert mapped.detail == {"error": {"message": "engine unavailable"}}
    assert mapped.headers == {"Retry-After": "2"}


def test_map_serving_exception_reads_status_from_response():
    mapped = map_serving_exception(
        _ResponseError(
            401,
            {"error": {"message": "unauthorized"}},
            headers={"WWW-Authenticate": "Bearer"},
        )
    )

    assert mapped.status_code == 401
    assert mapped.detail == {"error": {"message": "unauthorized"}}
    assert mapped.headers == {"WWW-Authenticate": "Bearer"}


def test_map_serving_exception_ignores_async_response_json():
    mapped = map_serving_exception(_AsyncResponseError())

    assert mapped.status_code == 422
    assert mapped.detail == "async json fallback"


def test_map_serving_exception_falls_back_when_json_payload_is_not_serializable():
    mapped = map_serving_exception(_NonJSONResponseError())

    assert mapped.status_code == 400
    assert mapped.detail == "plain fallback"


def test_map_serving_exception_rejects_non_finite_json_numbers():
    mapped = map_serving_exception(_NonFiniteJSONResponseError())

    assert mapped.status_code == 400
    assert mapped.detail == "non-finite fallback"


def test_map_serving_exception_does_not_leak_unawaited_coroutines(recwarn):
    map_serving_exception(_AsyncResponseError())

    asyncio.run(asyncio.sleep(0))
    assert not [warning for warning in recwarn if "was never awaited" in str(warning.message)]


@pytest.mark.parametrize(
    "status_code",
    [400, 401, 403, 404, 408, 409, 413, 415, 422, 429, 500, 502, 503, 504],
)
def test_map_serving_exception_preserves_http_error_status_matrix(status_code):
    mapped = map_serving_exception(_StatusError(status_code, "engine error"))

    assert mapped.status_code == status_code


@pytest.mark.parametrize(
    "error",
    [
        OverflowError("token count overflow"),
        BadRequestError("invalid sampling parameter"),
        ValueError("This model's maximum context length is 2048 tokens. However, you requested 2049 tokens."),
        ValueError("The sequence length is longer than the configured model limit"),
    ],
)
def test_map_serving_exception_maps_known_client_errors_to_400(error):
    mapped = map_serving_exception(error)

    assert mapped.status_code == 400
    assert mapped.detail == str(error)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("engine crashed"),
        ValueError("internal tensor shape mismatch"),
        _StatusError(302, "not an error status"),
    ],
)
def test_map_serving_exception_maps_unknown_failures_to_500(error):
    mapped = map_serving_exception(error)

    assert mapped.status_code == 500
    assert mapped.detail == str(error)


def test_map_serving_exception_sanitizes_unknown_failure_detail():
    mapped = map_serving_exception(RuntimeError(r"failed to open C:\secret\model.json"))

    assert mapped.status_code == 500
    assert mapped.detail == "failed to open [FILE_PATH]"


def test_map_serving_exception_can_leave_unknown_failure_for_dispatch_adapter():
    original = RuntimeError("engine boom")

    mapped = map_serving_exception(
        original,
        map_unknown_to_http_500=False,
    )

    assert mapped is original
