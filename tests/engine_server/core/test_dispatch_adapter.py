import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from motor.common.resources.dispatch import (
    MOTOR_DISPATCH_KEY,
    MOTOR_PREFILL_RESULT_KEY,
    DispatchStopState,
)
from motor.engine_server.core.dispatch_adapter.base import DispatchResponseContext
from motor.engine_server.core.dispatch_adapter.normalization import strip_engine_dispatch_fields
from motor.engine_server.core.dispatch_adapter.sglang_adapter import SGLangDispatchAdapter
from motor.engine_server.core.dispatch_adapter.vllm_adapter import VLLMDispatchAdapter


class _EngineConfig:
    def __init__(self, configs):
        self.configs = configs

    def get(self, key, default=None):
        return self.configs.get(key, default)


class _Config:
    def __init__(self, engine_type="vllm", role="decode", engine_config=None, dispatch_profile=None):
        self._endpoint_config = SimpleNamespace(
            engine_type=engine_type,
            role=role,
            deploy_config=SimpleNamespace(
                engine_config=_EngineConfig(engine_config or {}),
                infer_tls_config=None,
                dispatch_profile=dispatch_profile,
            ),
        )

    def get_endpoint_config(self):
        return self._endpoint_config


def _body(role="decode"):
    return {
        "model": "m",
        "prompt": "hello",
        MOTOR_DISPATCH_KEY: {
            "schema_version": "1.0",
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "pair_id": "pair",
            "attempt_seq": 1,
            "role": role,
            "dispatch_mode": "cdp_separate",
            "endpoints": {
                "prefill": {
                    "instance_id": 1,
                    "endpoint_id": 0,
                    "url": "http://127.0.0.1:8000",
                },
                "decode": {
                    "instance_id": 2,
                    "endpoint_id": 0,
                    "url": "http://127.0.0.2:8000",
                },
            },
        },
    }


def _cpcd_body(role="decode"):
    body = _body(role)
    body[MOTOR_DISPATCH_KEY]["dispatch_mode"] = "cpcd_separate"
    return body


def _handoff_config():
    return {
        "kv_transfer_config": {
            "kv_connector": "MooncakeConnectorV1",
            "kv_connector_module_path": "vllm_ascend.distributed.mooncake_connector",
        }
    }


def _hybrid_handoff_config():
    return {
        "kv_transfer_config": {
            "kv_connector": "MooncakeHybridConnector",
        }
    }


def _nixl_handoff_config():
    return {
        "kv_transfer_config": {
            "kv_connector": "NixlConnector",
        }
    }


def _multi_hybrid_handoff_config():
    return {
        "kv_transfer_config": {
            "kv_connector": "MultiConnector",
            "kv_connector_extra_config": {
                "connectors": [
                    {"kv_connector": "MooncakeHybridConnector"},
                    {"kv_connector": "AscendStoreConnector"},
                ]
            },
        }
    }


def _unknown_connector_v1_config():
    return {
        "kv_transfer_config": {
            "kv_connector": "LMCacheConnectorV1",
            "kv_connector_module_path": "vllm_ascend.distributed.mooncake_connector",
        }
    }


def _metaserver_trigger(request_id="req#a1"):
    return {
        "request_id": request_id,
        "do_remote_prefill": False,
        "do_remote_decode": True,
        "remote_block_ids": [[1, 2, 3]],
        "remote_block_size": [16],
        "remote_engine_id": "decode-engine",
        "remote_host": "127.0.0.2",
        "remote_port": 9000,
        "remote_cached_tokens": 32,
    }


def _context(dispatch, *, client_return_token_ids=False, chat=False):
    return DispatchResponseContext(
        api="v1/chat/completions" if chat else "v1/completions",
        raw_path="/v1/chat/completions" if chat else "/v1/completions",
        request_body={"messages": []} if chat else {"prompt": "hello"},
        dispatch=dispatch,
        stream=False,
        client_return_token_ids=client_return_token_ids,
        client_expects_chat_shape=chat,
    )


@pytest.mark.asyncio
async def test_vllm_decode_adapter_strips_dispatch_and_injects_metaserver():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    engine_body, dispatch = await adapter.adapt_request_body(_body("decode"))

    assert dispatch.root_request_id == "req"
    assert MOTOR_DISPATCH_KEY not in engine_body
    assert engine_body["request_id"] == "req#a1"
    assert engine_body["kv_transfer_params"]["metaserver"] == "http://127.0.0.1:8000/v1/metaserver"


def test_normalization_strips_engine_dispatch_fields():
    body = {
        "prompt": [1, 2, 3],
        "kv_transfer_params": {"metaserver": "legacy"},
        "bootstrap_host": "127.0.0.1",
        "bootstrap_port": 30000,
        "bootstrap_room": 7,
    }
    strip_engine_dispatch_fields(body)
    assert body == {"prompt": [1, 2, 3]}


@pytest.mark.asyncio
async def test_dispatch_adapter_rejects_role_mismatch():
    adapter = VLLMDispatchAdapter(_Config(role="prefill"))
    with pytest.raises(HTTPException):
        await adapter.adapt_request_body(_body("decode"))


@pytest.mark.asyncio
async def test_dispatch_adapter_rejects_legacy_fields_with_motor_dispatch():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    body = _body("decode")
    body["kv_transfer_params"] = {"metaserver": "legacy"}

    with pytest.raises(HTTPException):
        await adapter.adapt_request_body(body)


@pytest.mark.asyncio
async def test_dispatch_stop_is_idempotent():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    await adapter.adapt_request_body(_body("decode"))

    stop_body = {
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "attempt_seq": 1,
        "pair_id": "pair",
        "reason": "peer_failed",
    }
    first = await adapter.handle_stop(stop_body)
    second = await adapter.handle_stop(stop_body)

    assert first.state == DispatchStopState.STOPPED
    assert second.state == DispatchStopState.ALREADY_STOPPED


@pytest.mark.asyncio
async def test_dispatch_stop_rejects_stale_pair():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    await adapter.adapt_request_body(_body("decode"))

    stale = await adapter.handle_stop(
        {
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "attempt_seq": 1,
            "pair_id": "wrong-pair",
            "reason": "peer_failed",
        }
    )
    current = await adapter.handle_stop(
        {
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "attempt_seq": 1,
            "pair_id": "pair",
            "reason": "peer_failed",
        }
    )

    assert stale.state == DispatchStopState.STALE
    assert current.state == DispatchStopState.STOPPED


@pytest.mark.asyncio
async def test_dispatch_stop_after_finish_reports_already_done():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    _, dispatch = await adapter.adapt_request_body(_body("decode"))
    await adapter.finish_dispatch(dispatch)

    response = await adapter.handle_stop(
        {
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "attempt_seq": 1,
            "pair_id": "pair",
            "reason": "peer_failed",
        }
    )

    assert response.state == DispatchStopState.ALREADY_DONE


@pytest.mark.asyncio
async def test_vllm_prefill_prepare_and_metaserver_body(monkeypatch):
    peer_stops = []

    async def _stop_peer(self, dispatch, reason=None):
        peer_stops.append({"dispatch": dispatch, "reason": reason})
        return None

    monkeypatch.setattr(VLLMDispatchAdapter, "stop_peer", _stop_peer)
    adapter = VLLMDispatchAdapter(_Config(role="prefill"))
    body = _body("prefill")
    body["max_completion_tokens"] = 8
    engine_body, dispatch = await adapter.adapt_request_body(body)

    prepared = await adapter.maybe_prepare_response(engine_body, dispatch)
    assert prepared["object"] == "motor.prefill_result"
    assert prepared["status"] == "prepared"
    assert prepared["handoff_mode"] == "trigger"
    assert engine_body["min_tokens"] == 1
    assert engine_body["max_tokens"] == 1
    assert engine_body["max_completion_tokens"] == 1

    metaserver_body = await adapter.prepare_metaserver_body(_metaserver_trigger())
    assert metaserver_body["request_id"] == "req#a1"
    assert metaserver_body["kv_transfer_params"]["remote_block_ids"] == [[1, 2, 3]]
    assert metaserver_body["kv_transfer_params"]["do_remote_decode"] is True

    with pytest.raises(HTTPException):
        await adapter.prepare_metaserver_body({"request_id": "req#a1"})
    assert peer_stops[0]["dispatch"].engine_request_id == "req#a1"


@pytest.mark.asyncio
async def test_vllm_metaserver_trims_vllm_request_id_prefixes():
    adapter = VLLMDispatchAdapter(_Config(role="prefill"))
    engine_body, dispatch = await adapter.adapt_request_body(_body("prefill"))
    await adapter.maybe_prepare_response(engine_body, dispatch)

    metaserver_body = await adapter.prepare_metaserver_body(_metaserver_trigger("chatcmpl-req#a1"))
    assert metaserver_body["request_id"] == "req#a1"


@pytest.mark.asyncio
async def test_vllm_metaserver_accepts_nested_kv_transfer_params():
    adapter = VLLMDispatchAdapter(_Config(role="prefill"))
    engine_body, dispatch = await adapter.adapt_request_body(_body("prefill"))
    await adapter.maybe_prepare_response(engine_body, dispatch)

    metaserver_body = await adapter.prepare_metaserver_body(
        {"kv_transfer_params": _metaserver_trigger("chatcmpl-req#a1")}
    )

    assert metaserver_body["request_id"] == "req#a1"
    assert "kv_transfer_params" not in metaserver_body["kv_transfer_params"]
    assert metaserver_body["kv_transfer_params"]["remote_host"] == "127.0.0.2"


@pytest.mark.asyncio
async def test_vllm_metaserver_waits_for_late_prefill_cache():
    adapter = VLLMDispatchAdapter(_Config(role="prefill"))

    metaserver_task = asyncio.create_task(adapter.prepare_metaserver_body(_metaserver_trigger("chatcmpl-req#a1")))
    await asyncio.sleep(0)

    engine_body, dispatch = await adapter.adapt_request_body(_body("prefill"))
    await adapter.maybe_prepare_response(engine_body, dispatch)
    metaserver_body = await metaserver_task

    assert metaserver_body["request_id"] == "req#a1"
    assert metaserver_body["kv_transfer_params"]["remote_host"] == "127.0.0.2"


@pytest.mark.asyncio
async def test_vllm_metaserver_late_prefill_cache_timeout(monkeypatch):
    monkeypatch.setattr(VLLMDispatchAdapter, "_METASERVER_PREFILL_WAIT_SECONDS", 0.01)
    adapter = VLLMDispatchAdapter(_Config(role="prefill"))

    with pytest.raises(HTTPException) as exc:
        await adapter.prepare_metaserver_body(_metaserver_trigger("chatcmpl-missing#a1"))

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_vllm_dispatch_round_trip_decode_metaserver_to_prefill_cache():
    prefill_adapter = VLLMDispatchAdapter(_Config(role="prefill"))
    decode_adapter = VLLMDispatchAdapter(_Config(role="decode"))

    prefill_body, prefill_dispatch = await prefill_adapter.adapt_request_body(_body("prefill"))
    decode_body, _ = await decode_adapter.adapt_request_body(_body("decode"))
    await prefill_adapter.maybe_prepare_response(prefill_body, prefill_dispatch)

    trigger_body = _metaserver_trigger(decode_body["request_id"])
    metaserver_body = await prefill_adapter.prepare_metaserver_body(trigger_body)

    assert decode_body["kv_transfer_params"]["metaserver"].endswith("/v1/metaserver")
    assert metaserver_body["request_id"] == "req#a1"
    assert metaserver_body["kv_transfer_params"]["remote_host"] == "127.0.0.2"
    assert MOTOR_DISPATCH_KEY not in metaserver_body


@pytest.mark.asyncio
async def test_vllm_cpcd_prefill_response_becomes_completed_prefill_result():
    adapter = VLLMDispatchAdapter(_Config(role="prefill"))
    _, dispatch = await adapter.adapt_request_body(_cpcd_body("prefill"))
    response = JSONResponse({"kv": "opaque"})

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert '"object":"motor.prefill_result"' in body
    assert '"status":"completed"' in body
    assert '"handoff_mode":"handoff"' in body
    assert '"payload":{"kv":"opaque"}' in body


@pytest.mark.asyncio
async def test_vllm_handoff_profile_ignores_legacy_dispatch_mode_for_prefill_result():
    adapter = VLLMDispatchAdapter(_Config(role="prefill", engine_config=_handoff_config()))
    _, dispatch = await adapter.adapt_request_body(_body("prefill"))
    response = JSONResponse({"kv": "opaque"})

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert '"object":"motor.prefill_result"' in body
    assert '"status":"completed"' in body
    assert '"handoff_mode":"handoff"' in body


@pytest.mark.asyncio
async def test_vllm_hybrid_connector_uses_handoff_profile():
    adapter = VLLMDispatchAdapter(_Config(role="prefill", engine_config=_hybrid_handoff_config()))
    _, dispatch = await adapter.adapt_request_body(_body("prefill"))
    response = JSONResponse({"kv": "opaque"})

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert '"object":"motor.prefill_result"' in body
    assert '"handoff_mode":"handoff"' in body


@pytest.mark.asyncio
async def test_vllm_nixl_connector_uses_handoff_profile():
    adapter = VLLMDispatchAdapter(_Config(role="prefill", engine_config=_nixl_handoff_config()))
    _, dispatch = await adapter.adapt_request_body(_body("prefill"))
    response = JSONResponse({"kv": "opaque"})

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert '"object":"motor.prefill_result"' in body
    assert '"handoff_mode":"handoff"' in body


@pytest.mark.asyncio
async def test_vllm_multi_connector_uses_transport_connector_handoff_profile():
    adapter = VLLMDispatchAdapter(_Config(role="prefill", engine_config=_multi_hybrid_handoff_config()))
    _, dispatch = await adapter.adapt_request_body(_body("prefill"))
    response = JSONResponse({"kv": "opaque"})

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert '"object":"motor.prefill_result"' in body
    assert '"handoff_mode":"handoff"' in body


@pytest.mark.asyncio
async def test_vllm_unknown_connector_v1_is_not_inferred_as_handoff():
    """Connector name is authoritative even when module_path resembles Mooncake."""
    adapter = VLLMDispatchAdapter(_Config(role="prefill", engine_config=_unknown_connector_v1_config()))
    _, dispatch = await adapter.adapt_request_body(_body("prefill"))
    response = JSONResponse({"choices": [{"text": "ok"}]})

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert "motor.prefill_result" not in body
    assert '"choices":[{"text":"ok"}]' in body


@pytest.mark.asyncio
async def test_vllm_explicit_dispatch_profile_enables_handoff_for_unknown_connector():
    adapter = VLLMDispatchAdapter(
        _Config(
            role="prefill",
            engine_config=_unknown_connector_v1_config(),
            dispatch_profile="handoff",
        )
    )
    _, dispatch = await adapter.adapt_request_body(_body("prefill"))
    response = JSONResponse({"kv": "opaque"})

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert '"object":"motor.prefill_result"' in body
    assert '"handoff_mode":"handoff"' in body


@pytest.mark.asyncio
async def test_vllm_cpcd_decode_consumes_prefill_result_payload():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    body = _cpcd_body("decode")
    body[MOTOR_PREFILL_RESULT_KEY] = {
        "object": "motor.prefill_result",
        "schema_version": "1.0",
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "pair_id": "pair",
        "attempt_seq": 1,
        "status": "completed",
        "handoff_mode": "handoff",
        "payload": {"remote_block_ids": [1, 2]},
    }

    engine_body, _ = await adapter.adapt_request_body(body)

    assert MOTOR_PREFILL_RESULT_KEY not in engine_body
    assert engine_body["kv_transfer_params"] == {"remote_block_ids": [1, 2]}


@pytest.mark.asyncio
async def test_vllm_handoff_profile_consumes_prefill_result_without_cpcd_dispatch_mode():
    adapter = VLLMDispatchAdapter(_Config(role="decode", engine_config=_handoff_config()))
    body = _body("decode")
    body[MOTOR_PREFILL_RESULT_KEY] = {
        "object": "motor.prefill_result",
        "schema_version": "1.0",
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "pair_id": "pair",
        "attempt_seq": 1,
        "status": "completed",
        "handoff_mode": "handoff",
        "payload": {"remote_block_ids": [1, 2]},
    }

    engine_body, _ = await adapter.adapt_request_body(body)

    assert MOTOR_PREFILL_RESULT_KEY not in engine_body
    assert engine_body["kv_transfer_params"] == {"remote_block_ids": [1, 2]}


@pytest.mark.asyncio
async def test_dispatch_adapter_setup_error_stops_peer(monkeypatch):
    peer_stops = []

    async def _stop_peer(self, dispatch, reason=None):
        peer_stops.append(dispatch)
        return None

    monkeypatch.setattr(VLLMDispatchAdapter, "stop_peer", _stop_peer)
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    body = _cpcd_body("decode")
    body[MOTOR_PREFILL_RESULT_KEY] = {
        "object": "motor.prefill_result",
        "schema_version": "1.0",
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "pair_id": "pair",
        "attempt_seq": 1,
        "status": "prepared",
        "handoff_mode": "trigger",
        "payload": {},
    }

    with pytest.raises(HTTPException):
        await adapter.adapt_request_body(body)

    assert peer_stops[0].engine_request_id == "req#a1"


@pytest.mark.asyncio
async def test_vllm_normalizes_nonstream_dispatch_response():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    _, dispatch = await adapter.adapt_request_body(_body("decode"))
    response = JSONResponse(
        {
            "prompt_token_ids": [1, 2],
            "choices": [
                {
                    "text": "ok",
                    "token_ids": [3],
                    "prompt_token_ids": [1, 2],
                    "stop_reason": "recomputed",
                }
            ],
        }
    )

    normalized = await adapter.normalize_response(response, _context(dispatch))
    body = normalized.body.decode("utf-8")

    assert "token_ids" not in body
    assert "prompt_token_ids" not in body
    assert '"stop_reason":"stop"' in body


@pytest.mark.asyncio
async def test_vllm_normalizes_completion_response_to_chat_shape():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    _, dispatch = await adapter.adapt_request_body(_body("decode"))
    response = JSONResponse(
        {
            "object": "text_completion",
            "choices": [{"text": "hello", "token_ids": [3]}],
        }
    )

    normalized = await adapter.normalize_response(response, _context(dispatch, chat=True))
    body = normalized.body.decode("utf-8")

    assert '"object":"chat.completion"' in body
    assert '"message":{"role":"assistant","content":"hello"}' in body
    assert "token_ids" not in body


@pytest.mark.asyncio
async def test_vllm_normalizes_stream_dispatch_chunk():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    _, dispatch = await adapter.adapt_request_body(_body("decode"))

    chunk = await adapter.normalize_stream_chunk(
        b'data: {"choices":[{"text":"A","token_ids":[1],"stop_reason":"recomputed"}]}\n\n',
        _context(dispatch, chat=True),
        {},
    )

    assert b'"object":"chat.completion.chunk"' in chunk
    assert b'"delta":{"role":"assistant","content":"A"}' in chunk
    assert b"token_ids" not in chunk
    assert b'"stop_reason":"stop"' in chunk


@pytest.mark.asyncio
async def test_vllm_stream_normalization_preserves_done():
    adapter = VLLMDispatchAdapter(_Config(role="decode"))
    _, dispatch = await adapter.adapt_request_body(_body("decode"))

    chunk = await adapter.normalize_stream_chunk(
        b"data: [DONE]\n\n",
        _context(dispatch),
        {},
    )

    assert chunk == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_sglang_adapter_generates_stable_bootstrap(monkeypatch):
    monkeypatch.setenv("DISAGGREGATION_BOOTSTRAP_PORT", "31000")
    adapter = SGLangDispatchAdapter(_Config(engine_type="sglang", role="decode"))

    first, _ = await adapter.adapt_request_body(_body("decode"))
    second, _ = await adapter.adapt_request_body(_body("decode"))

    assert first["bootstrap_host"] == "127.0.0.1"
    assert first["bootstrap_port"] == "31000"
    assert first["bootstrap_room"] == second["bootstrap_room"]


@pytest.mark.asyncio
async def test_sglang_adapter_requires_bootstrap_port(monkeypatch):
    peer_stops = []

    async def _stop_peer(self, dispatch, reason=None):
        peer_stops.append(dispatch)
        return None

    monkeypatch.delenv("DISAGGREGATION_BOOTSTRAP_PORT", raising=False)
    monkeypatch.setattr(SGLangDispatchAdapter, "stop_peer", _stop_peer)
    adapter = SGLangDispatchAdapter(_Config(engine_type="sglang", role="decode"))

    with pytest.raises(HTTPException) as exc_info:
        await adapter.adapt_request_body(_body("decode"))

    assert exc_info.value.status_code == 500
    assert "DISAGGREGATION_BOOTSTRAP_PORT" in exc_info.value.detail
    assert peer_stops[0].engine_request_id == "req#a1"
