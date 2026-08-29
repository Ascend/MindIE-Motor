# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from copy import deepcopy

import pytest

from motor.coordinator.router.adapters.pd_protocol import (
    ADAPTERS,
    CoordinationMode,
    EngineEndpointMetadata,
    EngineLegSpec,
    EnginePhase,
    EngineProtocolError,
    GenerationConstraint,
    KVTransferDescriptor,
    LegContext,
    PrefillMetadata,
    SglangProtocolAdapter,
    NATIVE_GENERATE_API,
    VllmProtocolAdapter,
)


def _endpoint(host: str, port: int | None = None) -> EngineEndpointMetadata:
    return EngineEndpointMetadata(
        host=host,
        bootstrap_port=port,
    )


def _context(
    *,
    endpoint: EngineEndpointMetadata | None = None,
    peer_endpoint: EngineEndpointMetadata | None = None,
    attempt_seq: int = 2,
) -> LegContext:
    return LegContext(
        engine_request_id="engine-1",
        pair_id="pair-1",
        attempt_seq=attempt_seq,
        api="/v1/chat/completions",
        endpoint=endpoint or _endpoint("prefill.local", 8998),
        peer_endpoint=peer_endpoint,
    )


def test_adapter_registry_is_static():
    assert ADAPTERS["vllm"].coordination_mode is CoordinationMode.HANDOFF
    assert ADAPTERS["sglang"].coordination_mode is CoordinationMode.BOOTSTRAP

    with pytest.raises(TypeError):
        ADAPTERS["other"] = VllmProtocolAdapter()


def test_vllm_prefill_request_matches_native_handoff_contract_without_mutation():
    adapter = VllmProtocolAdapter()
    request = {
        "model": "glm",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 128,
        "max_completion_tokens": 96,
        "request_id": "client-request-id",
        "rid": "wrong-engine-id",
        "extra": {"nested": [1, 2]},
    }
    original = deepcopy(request)

    engine_request = adapter.build_prefill_request(request, _context())

    assert request == original
    assert engine_request.api == "/v1/chat/completions"
    assert engine_request.body["request_id"] == "engine-1"
    assert "rid" not in engine_request.body
    assert engine_request.body["stream"] is False
    assert engine_request.body["max_tokens"] == 1
    assert engine_request.body["max_completion_tokens"] == 1
    assert engine_request.body["min_tokens"] == 1
    assert "stream_options" not in engine_request.body
    assert engine_request.body["kv_transfer_params"] == {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "remote_engine_id": None,
        "remote_block_ids": None,
        "remote_host": None,
        "remote_port": None,
    }
    engine_request.body["extra"]["nested"].append(3)
    assert request == original


def test_vllm_prefill_does_not_add_max_completion_tokens():
    request = {"max_tokens": 32}

    body = VllmProtocolAdapter().build_prefill_request(request, _context()).body

    assert "max_completion_tokens" not in body


def _token_metadata():
    return {
        "request_id": "render-request",
        "model": "glm",
        "sampling_params": {
            "max_tokens": 8,
            "temperature": 0.2,
            "extra_args": {"render_flag": "keep"},
        },
        "features": {"mm_hashes": {"image": ["hash"]}},
        "kv_transfer_params": {"stale": True},
    }


PREFILL_KV = {"do_remote_decode": True, "do_remote_prefill": False}
DECODE_KV = {"do_remote_prefill": True, "remote_host": "10.0.0.1"}


@pytest.mark.parametrize(
    ("builder", "expected_kv", "expected_max_tokens"),
    [
        (
            lambda a, t, m, c: a.build_tokenized_request(
                t,
                m,
                EngineLegSpec(
                    context=c,
                    phase=EnginePhase.PREFILL,
                    generation=GenerationConstraint(max_tokens=1, min_tokens=1),
                    kv_transfer=KVTransferDescriptor(PREFILL_KV),
                ),
            ),
            PREFILL_KV,
            1,
        ),
        (
            lambda a, t, m, c: a.build_tokenized_request(
                t,
                m,
                EngineLegSpec(
                    context=c,
                    phase=EnginePhase.DECODE,
                    kv_transfer=KVTransferDescriptor(DECODE_KV),
                ),
            ),
            DECODE_KV,
            8,
        ),
        (
            lambda a, t, m, c: a.build_tokenized_request(
                t,
                m,
                EngineLegSpec(context=c, phase=EnginePhase.DECODE),
            ),
            None,
            8,
        ),
    ],
)
def test_vllm_tokenized_builders_preserve_metadata_and_shape(builder, expected_kv, expected_max_tokens):
    metadata = _token_metadata()
    original = deepcopy(metadata)
    request = builder(VllmProtocolAdapter(), [10, 20], metadata, _context())

    assert metadata == original
    assert request.api == NATIVE_GENERATE_API
    assert request.body["request_id"] == "engine-1"
    assert request.body["token_ids"] == [10, 20]
    assert request.body["stream"] is False
    assert request.body["sampling_params"]["max_tokens"] == expected_max_tokens
    assert request.body["sampling_params"]["extra_args"]["render_flag"] == "keep"
    assert request.body["features"] == metadata["features"]
    if expected_max_tokens == 1:
        assert request.body["sampling_params"]["min_tokens"] == 1
    if expected_kv is None:
        assert "kv_transfer_params" not in request.body
        assert "kv_transfer_params" not in request.body["sampling_params"]["extra_args"]
    else:
        assert request.body["kv_transfer_params"] == expected_kv
        assert request.body["sampling_params"]["extra_args"]["kv_transfer_params"] == expected_kv


def test_vllm_tokenized_request_requires_render_sampling_params():
    leg = EngineLegSpec(
        context=_context(),
        phase=EnginePhase.PREFILL,
        generation=GenerationConstraint(max_tokens=1, min_tokens=1),
        kv_transfer=KVTransferDescriptor(PREFILL_KV),
    )

    with pytest.raises(EngineProtocolError, match="missing sampling_params"):
        VllmProtocolAdapter().build_tokenized_request([10], {}, leg)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": "invalid"},
        {"choices": [{}]},
        {"choices": [{"token_ids": [1, True]}]},
    ],
)
def test_vllm_tokenized_decode_rejects_invalid_generate_response(response):
    with pytest.raises(EngineProtocolError, match="invalid"):
        VllmProtocolAdapter().validate_tokenized_decode_response(response)


def test_vllm_prefill_response_returns_copied_ticket_and_usage():
    response = {
        "kv_transfer_params": {
            "do_remote_prefill": True,
            "remote_host": "10.0.0.1",
            "connector_private": {"blocks": [1, 2]},
        },
        "usage": {"prompt_tokens": 8, "details": {"cached_tokens": 4}},
    }

    metadata = VllmProtocolAdapter().parse_prefill_response(response)

    assert metadata.handoff_ticket == response["kv_transfer_params"]
    assert metadata.usage == response["usage"]
    response["kv_transfer_params"]["connector_private"]["blocks"].append(3)
    response["usage"]["details"]["cached_tokens"] = 0
    assert metadata.handoff_ticket["connector_private"]["blocks"] == [1, 2]
    assert metadata.usage["details"]["cached_tokens"] == 4


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({}, "Missing kv_transfer_params"),
        ({"kv_transfer_params": {}}, "Missing kv_transfer_params"),
        ({"kv_transfer_params": {"do_remote_prefill": False}}, "do_remote_prefill must be true"),
    ],
)
def test_vllm_prefill_response_rejects_invalid_handoff(response, message):
    with pytest.raises(EngineProtocolError, match=message) as exc_info:
        VllmProtocolAdapter().parse_prefill_response(response)

    assert exc_info.value.engine_type == "vllm"
    assert exc_info.value.phase == "prefill"


def test_vllm_decode_restores_original_generation_budget_and_copies_ticket():
    adapter = VllmProtocolAdapter()
    request = {
        "stream": True,
        "max_tokens": 128,
        "max_completion_tokens": 96,
        "extra": {"nested": [1]},
    }
    original = deepcopy(request)
    ticket = {"do_remote_prefill": True, "remote_host": "10.0.0.1", "private": {"blocks": [7]}}

    engine_request = adapter.build_decode_request(
        request,
        _context(),
        PrefillMetadata(handoff_ticket=ticket),
    )

    assert request == original
    assert engine_request.body["request_id"] == "engine-1"
    assert engine_request.body["stream"] is True
    assert engine_request.body["max_tokens"] == 128
    assert engine_request.body["max_completion_tokens"] == 96
    assert engine_request.body["kv_transfer_params"] == ticket
    engine_request.body["kv_transfer_params"]["private"]["blocks"].append(8)
    assert ticket["private"]["blocks"] == [7]


@pytest.mark.parametrize("metadata", [None, PrefillMetadata(), PrefillMetadata(handoff_ticket={})])
def test_vllm_decode_requires_handoff_ticket(metadata):
    with pytest.raises(EngineProtocolError, match="Missing handoff ticket") as exc_info:
        VllmProtocolAdapter().build_decode_request({}, _context(), metadata)

    assert exc_info.value.phase == "decode"


def test_vllm_declares_internal_response_field():
    assert VllmProtocolAdapter.internal_response_fields == frozenset({"kv_transfer_params"})


def test_sglang_prefill_and_decode_share_prefill_bootstrap_address_and_room():
    adapter = SglangProtocolAdapter()
    prefill_endpoint = _endpoint("prefill.local", 8998)
    decode_endpoint = _endpoint("decode.local", 8999)
    request = {
        "model": "glm",
        "messages": [{"role": "user", "content": "hello"}],
        "request_id": "wrong-engine-id",
        "rid": "client-request-id",
    }
    original = deepcopy(request)
    prefill_context = _context(endpoint=prefill_endpoint, peer_endpoint=decode_endpoint)
    decode_context = _context(endpoint=decode_endpoint, peer_endpoint=prefill_endpoint)

    prefill = adapter.build_prefill_request(request, prefill_context)
    decode = adapter.build_decode_request(request, decode_context, None)

    assert request == original
    assert prefill.body["rid"] == "engine-1"
    assert decode.body["rid"] == "engine-1"
    assert "request_id" not in prefill.body
    assert "request_id" not in decode.body
    assert prefill.body["stream"] is False
    for body in (prefill.body, decode.body):
        assert body["bootstrap_host"] == "prefill.local"
        assert body["bootstrap_port"] == 8998
    assert prefill.body["bootstrap_room"] == decode.body["bootstrap_room"]
    prefill.body["messages"][0]["content"] = "changed"
    assert request == original


def test_sglang_bootstrap_room_is_stable_per_attempt():
    adapter = SglangProtocolAdapter()

    first = adapter.build_prefill_request({}, _context(attempt_seq=2)).body["bootstrap_room"]
    repeated = adapter.build_prefill_request({}, _context(attempt_seq=2)).body["bootstrap_room"]
    retried = adapter.build_prefill_request({}, _context(attempt_seq=3)).body["bootstrap_room"]

    assert first == repeated
    assert first != retried
    assert 0 <= first < 1 << 63


@pytest.mark.parametrize(
    "endpoint",
    [
        EngineEndpointMetadata(host="", bootstrap_port=8998),
        _endpoint("prefill.local"),
        _endpoint("prefill.local", 0),
        _endpoint("prefill.local", 65536),
    ],
)
def test_sglang_prefill_rejects_invalid_bootstrap_endpoint(endpoint):
    with pytest.raises(EngineProtocolError) as exc_info:
        SglangProtocolAdapter().build_prefill_request({}, _context(endpoint=endpoint))

    assert exc_info.value.engine_type == "sglang"
    assert exc_info.value.phase == "prefill"


def test_sglang_decode_requires_prefill_endpoint():
    with pytest.raises(EngineProtocolError, match="Missing prefill endpoint metadata") as exc_info:
        SglangProtocolAdapter().build_decode_request({}, _context(), None)

    assert exc_info.value.phase == "decode"


def test_sglang_prefill_response_copies_usage_without_handoff_ticket():
    response = {"usage": {"prompt_tokens": 8, "details": {"cached_tokens": 4}}}

    metadata = SglangProtocolAdapter().parse_prefill_response(response)

    assert metadata.handoff_ticket is None
    assert metadata.usage == response["usage"]
    response["usage"]["details"]["cached_tokens"] = 0
    assert metadata.usage["details"]["cached_tokens"] == 4


def test_sglang_declares_internal_response_fields():
    assert SglangProtocolAdapter.internal_response_fields == frozenset(
        {"bootstrap_host", "bootstrap_port", "bootstrap_room"}
    )


def test_sglang_abort_uses_native_request_id():
    request = SglangProtocolAdapter().build_abort_request(_context())

    assert request.api == "abort_request"
    assert request.body == {"rid": "engine-1"}


def test_vllm_does_not_claim_an_unverified_abort_endpoint():
    assert VllmProtocolAdapter().build_abort_request(_context()) is None


def test_trim_vllm_engine_request_id_strips_openai_prefixes():
    from motor.coordinator.router.adapters.pd_protocol import trim_vllm_engine_request_id

    assert trim_vllm_engine_request_id("chatcmpl-abc") == "abc"
    assert trim_vllm_engine_request_id("cmpl-abc-0") == "abc"
    assert trim_vllm_engine_request_id("raw-id") == "raw-id"


def test_vllm_trigger_decode_request_keeps_generation_and_sets_metaserver():
    adapter = VllmProtocolAdapter()
    request = {
        "model": "glm",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "max_tokens": 128,
        "rid": "wrong",
    }
    original = deepcopy(request)

    engine_request = adapter.build_trigger_decode_request(request, _context(), "http://127.0.0.1:12000/v1/metaserver")

    assert request == original
    assert engine_request.body["request_id"] == "engine-1"
    assert engine_request.body["stream"] is True
    assert engine_request.body["max_tokens"] == 128
    assert engine_request.body["kv_transfer_params"] == {
        "do_remote_decode": False,
        "do_remote_prefill": True,
        "metaserver": "http://127.0.0.1:12000/v1/metaserver",
    }


def test_vllm_trigger_prefill_request_uses_decode_kv_params():
    adapter = VllmProtocolAdapter()
    request = {"model": "glm", "messages": [], "stream": True, "max_tokens": 32, "max_completion_tokens": 16}
    kv_params = {
        "request_id": "engine-1",
        "do_remote_decode": True,
        "remote_block_ids": [1, 2],
        "remote_host": "10.0.0.2",
        "metaserver": "http://ignored",
    }

    engine_request = adapter.build_trigger_prefill_request(request, _context(), kv_params)

    assert engine_request.body["stream"] is False
    assert engine_request.body["max_tokens"] == 1
    assert engine_request.body["min_tokens"] == 1
    assert engine_request.body["max_completion_tokens"] == 1
    assert engine_request.body["kv_transfer_params"]["do_remote_decode"] is True
    assert engine_request.body["kv_transfer_params"]["do_remote_prefill"] is False
    assert engine_request.body["kv_transfer_params"]["remote_block_ids"] == [1, 2]
    assert "metaserver" not in engine_request.body["kv_transfer_params"]
