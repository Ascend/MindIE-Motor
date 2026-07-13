# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
from types import SimpleNamespace

import pytest

from motor.engine_server.core.vllm.vllm_openai_compat import (
    build_openai_serving_render_kwargs,
    call_openai_serving,
    kwargs_matching_signature,
    openai_http_response_from_exception,
)


def test_kwargs_matching_signature_filters_unknown_keys():
    def target(a, *, b=None):
        return a, b

    result = kwargs_matching_signature(target, {"a": 1, "b": 2, "c": 3})
    assert result == {"a": 1, "b": 2}


def test_build_render_kwargs_without_io_processor_when_not_required():
    class RenderNoIoProcessor:
        def __init__(self, model_config, renderer, model_registry, request_logger=None):
            self.model_config = model_config
            self.renderer = renderer
            self.model_registry = model_registry
            self.request_logger = request_logger

    engine_client = SimpleNamespace(model_config="mcfg", renderer="rnd")
    base_kwargs = {
        "model_registry": "registry",
        "request_logger": "logger",
        "unused_field": "ignored",
    }

    kwargs = build_openai_serving_render_kwargs(
        RenderNoIoProcessor.__init__,
        engine_client,
        base_kwargs,
    )

    assert kwargs == {
        "model_config": "mcfg",
        "renderer": "rnd",
        "model_registry": "registry",
        "request_logger": "logger",
    }


def test_build_render_kwargs_maps_processor_to_io_processor():
    class RenderNeedIoProcessor:
        def __init__(self, model_config, renderer, io_processor, model_registry):
            self.model_config = model_config
            self.renderer = renderer
            self.io_processor = io_processor
            self.model_registry = model_registry

    engine_client = SimpleNamespace(model_config="mcfg", renderer="rnd", processor="proc")
    base_kwargs = {"model_registry": "registry"}

    kwargs = build_openai_serving_render_kwargs(
        RenderNeedIoProcessor.__init__,
        engine_client,
        base_kwargs,
    )

    assert kwargs["io_processor"] == "proc"
    assert kwargs["model_config"] == "mcfg"
    assert kwargs["renderer"] == "rnd"
    assert kwargs["model_registry"] == "registry"


def test_build_render_kwargs_does_not_map_tokenizer_to_io_processor():
    class RenderNeedIoProcessor:
        def __init__(self, model_config, renderer, io_processor, model_registry):
            self.model_config = model_config
            self.renderer = renderer
            self.io_processor = io_processor
            self.model_registry = model_registry

    engine_client = SimpleNamespace(model_config="mcfg", renderer="rnd", tokenizer="tok")
    base_kwargs = {"model_registry": "registry"}

    with pytest.raises(RuntimeError, match="missing required kwargs"):
        build_openai_serving_render_kwargs(
            RenderNeedIoProcessor.__init__,
            engine_client,
            base_kwargs,
        )


def test_build_render_kwargs_keeps_none_for_existing_required_attr():
    class RenderNeedIoProcessor:
        def __init__(self, model_config, renderer, io_processor, model_registry):
            self.model_config = model_config
            self.renderer = renderer
            self.io_processor = io_processor
            self.model_registry = model_registry

    engine_client = SimpleNamespace(model_config="mcfg", renderer="rnd", io_processor=None)
    base_kwargs = {"model_registry": "registry"}

    kwargs = build_openai_serving_render_kwargs(
        RenderNeedIoProcessor.__init__,
        engine_client,
        base_kwargs,
    )

    assert "io_processor" in kwargs
    assert kwargs["io_processor"] is None
    assert kwargs["model_config"] == "mcfg"
    assert kwargs["renderer"] == "rnd"
    assert kwargs["model_registry"] == "registry"


def test_build_render_kwargs_raise_on_missing_required():
    class RenderNeedIoProcessor:
        def __init__(self, model_config, renderer, io_processor, model_registry):
            self.model_config = model_config
            self.renderer = renderer
            self.io_processor = io_processor
            self.model_registry = model_registry

    engine_client = SimpleNamespace(model_config="mcfg", renderer="rnd")
    base_kwargs = {"model_registry": "registry"}

    with pytest.raises(RuntimeError, match="missing required kwargs"):
        build_openai_serving_render_kwargs(
            RenderNeedIoProcessor.__init__,
            engine_client,
            base_kwargs,
        )


class _ValidationError(ValueError):
    def __init__(self, message: str, *, parameter: str | None = None) -> None:
        super().__init__(message)
        self.parameter = parameter


def _mock_create_error_response(exc: Exception):
    if isinstance(exc, _ValidationError):
        return SimpleNamespace(
            model_dump=lambda: {
                "error": {
                    "message": str(exc),
                    "type": "BadRequestError",
                    "param": exc.parameter,
                    "code": 400,
                }
            },
            error=SimpleNamespace(code=400),
        )
    return SimpleNamespace(
        model_dump=lambda: {
            "error": {
                "message": str(exc),
                "type": "InternalServerError",
                "param": None,
                "code": 500,
            }
        },
        error=SimpleNamespace(code=500),
    )


def test_openai_http_response_from_exception_maps_validation_error_via_fallback():
    response = openai_http_response_from_exception(
        _ValidationError("max_tokens too large", parameter="max_tokens"),
    )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "BadRequestError"
    assert payload["error"]["param"] == "max_tokens"


@pytest.mark.asyncio
async def test_call_openai_serving_returns_vllm_error_response_on_exception():
    serving = SimpleNamespace(create_error_response=_mock_create_error_response)

    async def _raise():
        raise _ValidationError("prompt too long", parameter="input_tokens")

    with pytest.raises(_ValidationError):
        await call_openai_serving(serving, _raise, dict)


@pytest.mark.asyncio
async def test_call_openai_serving_propagates_unexpected_exception():
    serving = SimpleNamespace(create_error_response=_mock_create_error_response)

    async def _raise():
        raise RuntimeError("engine crashed")

    with pytest.raises(RuntimeError):
        await call_openai_serving(serving, _raise, dict)


def test_openai_http_response_from_exception_maps_http_exception_to_vllm_shape():
    from fastapi import HTTPException

    response = openai_http_response_from_exception(
        HTTPException(
            status_code=429,
            detail="engine rate limited",
            headers={"Retry-After": "3"},
        )
    )

    payload = json.loads(response.body)
    assert response.status_code == 429
    assert payload["error"]["code"] == 429
    assert payload["error"]["message"] == "engine rate limited"
    # vLLM HTTPException handler keeps the spaced HTTP phrase.
    assert payload["error"]["type"] == "Too Many Requests"
    assert response.headers["retry-after"] == "3"


def test_openai_http_response_from_exception_keeps_http_500_phrase_spacing():
    from fastapi import HTTPException

    http_response = openai_http_response_from_exception(HTTPException(status_code=500, detail="boom"))
    plain_response = openai_http_response_from_exception(RuntimeError("boom"))

    assert json.loads(http_response.body)["error"]["type"] == "Internal Server Error"
    assert json.loads(plain_response.body)["error"]["type"] == "InternalServerError"


def test_openai_http_response_from_exception_maps_runtime_error_to_500():
    response = openai_http_response_from_exception(RuntimeError("engine boom"))

    assert response.status_code == 500
    payload = response.body.decode("utf-8")
    assert "engine boom" in payload
    assert '"code":500' in payload or '"code": 500' in payload


def test_openai_http_response_from_exception_handles_nonstandard_status_code():
    from fastapi import HTTPException

    response = openai_http_response_from_exception(HTTPException(status_code=499, detail="Dispatch stopped by peer."))

    assert response.status_code == 499
    assert b'"type":"ClientClosedRequest"' in response.body


def test_openai_http_response_from_exception_sanitizes_fallback_message():
    from fastapi import HTTPException

    response = openai_http_response_from_exception(
        HTTPException(status_code=500, detail=r"failed to open C:\secret\model.json")
    )

    assert b"C:\\secret" not in response.body
    assert b"[FILE_PATH]" in response.body


def test_openai_http_response_from_exception_sanitizes_generic_fallback_message():
    response = openai_http_response_from_exception(RuntimeError(r"failed to open C:\secret\model.json"))

    assert b"C:\\secret" not in response.body
    assert b"[FILE_PATH]" in response.body


def test_openai_http_response_from_exception_preserves_validation_param():
    from fastapi import HTTPException

    response = openai_http_response_from_exception(
        HTTPException(
            status_code=400,
            detail=[{"loc": ("body", "max_tokens"), "msg": "Input should be a valid integer"}],
        )
    )

    payload = json.loads(response.body)
    assert payload["error"]["type"] == "Bad Request"
    assert payload["error"]["param"] == "body.max_tokens"


def test_openai_http_response_from_exception_prefers_ctx_vllm_validation_parameter():
    from fastapi import HTTPException

    class VLLMValidationError(ValueError):
        def __init__(self, message: str, *, parameter: str) -> None:
            super().__init__(message)
            self.parameter = parameter

    response = openai_http_response_from_exception(
        HTTPException(
            status_code=400,
            detail=[
                {
                    "loc": ("body", "function-wrap", "prompt"),
                    "msg": "Value error",
                    "ctx": {"error": VLLMValidationError("bad prompt", parameter="prompt")},
                }
            ],
        )
    )

    payload = json.loads(response.body)
    assert payload["error"]["param"] == "prompt"


@pytest.mark.parametrize(
    ("exc", "expected_type", "expected_code"),
    [
        (ValueError("bad"), "BadRequestError", 400),
        (TypeError("bad"), "BadRequestError", 400),
        (OverflowError("bad"), "BadRequestError", 400),
        (NotImplementedError("nope"), "NotImplementedError", 501),
        (RuntimeError("boom"), "InternalServerError", 500),
    ],
)
def test_fallback_error_classification_matrix(exc, expected_type, expected_code):
    from motor.engine_server.core.vllm.vllm_openai_compat import _fallback_error_classification

    assert _fallback_error_classification(exc) == (expected_type, expected_code)


def test_fallback_error_classification_named_vllm_errors():
    from motor.engine_server.core.vllm.vllm_openai_compat import _fallback_error_classification

    class VLLMValidationError(Exception):
        pass

    class VLLMUnprocessableEntityError(Exception):
        pass

    class VLLMNotFoundError(Exception):
        pass

    class GenerationError(Exception):
        def __init__(self, message, status_code=503):
            super().__init__(message)
            self.status_code = status_code

    class TemplateError(Exception):
        pass

    class TemplateSyntaxError(TemplateError):
        pass

    assert _fallback_error_classification(VLLMValidationError("x")) == ("BadRequestError", 400)
    assert _fallback_error_classification(VLLMUnprocessableEntityError("x")) == (
        "UnprocessableEntityError",
        422,
    )
    assert _fallback_error_classification(VLLMNotFoundError("x")) == ("NotFoundError", 404)
    assert _fallback_error_classification(GenerationError("x", status_code=503)) == (
        "InternalServerError",
        503,
    )
    assert _fallback_error_classification(TemplateSyntaxError("bad template")) == (
        "BadRequestError",
        400,
    )


def test_vllm_stream_error_json_for_http_exception():
    from fastapi import HTTPException

    from motor.engine_server.core.vllm.vllm_openai_compat import vllm_stream_error_json

    payload = json.loads(vllm_stream_error_json(HTTPException(status_code=499, detail="Dispatch stopped by peer.")))
    assert payload["error"]["code"] == 499
    assert payload["error"]["type"] == "ClientClosedRequest"
