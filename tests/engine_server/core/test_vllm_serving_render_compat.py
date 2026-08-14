# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Motor OpenAI serving wrappers must forward both render-layer kwargs across vLLM versions."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _ensure_module(name: str) -> types.ModuleType:
    existing = sys.modules.get(name)
    if isinstance(existing, types.ModuleType) and not isinstance(existing, MagicMock) and hasattr(existing, "__path__"):
        return existing
    mod = types.ModuleType(name)
    # Mark as a package so nested imports like ``a.b.c`` succeed.
    mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    parent_name, _, child = name.rpartition(".")
    if parent_name:
        parent = _ensure_module(parent_name)
        setattr(parent, child, mod)
    return mod


def _ensure_leaf_module(name: str) -> types.ModuleType:
    existing = sys.modules.get(name)
    if isinstance(existing, types.ModuleType) and not isinstance(existing, MagicMock):
        return existing
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    parent_name, _, child = name.rpartition(".")
    if parent_name:
        parent = _ensure_module(parent_name)
        setattr(parent, child, mod)
    return mod


@pytest.fixture
def serving_vllm_stubs(monkeypatch):
    """Install package-like vLLM stubs so Motor serving wrappers can import."""
    chat_serving = _ensure_leaf_module("vllm.entrypoints.openai.chat_completion.serving")
    completion_serving = _ensure_leaf_module("vllm.entrypoints.openai.completion.serving")
    chat_protocol = _ensure_leaf_module("vllm.entrypoints.openai.chat_completion.protocol")
    completion_protocol = _ensure_leaf_module("vllm.entrypoints.openai.completion.protocol")
    models_serving = _ensure_leaf_module("vllm.entrypoints.openai.models.serving")
    engine_protocol = _ensure_leaf_module("vllm.engine.protocol")
    chat_utils = _ensure_leaf_module("vllm.entrypoints.chat_utils")

    chat_serving.OpenAIServingChat = object
    completion_serving.OpenAIServingCompletion = object
    chat_protocol.ChatCompletionRequest = object
    chat_protocol.ChatCompletionResponse = object
    completion_protocol.CompletionRequest = object
    completion_protocol.CompletionResponse = object
    models_serving.OpenAIServingModels = object
    engine_protocol.EngineClient = object
    chat_utils.ChatTemplateContentFormatOption = object

    for mod_name in (
        "motor.engine_server.core.vllm.openai.serving_chat",
        "motor.engine_server.core.vllm.openai.serving_completion",
    ):
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    yield


def test_serving_chat_forwards_online_renderer(serving_vllm_stubs, monkeypatch):
    captured: dict = {}

    class FakeVllmOpenAIServingChat:
        def __init__(
            self,
            engine_client,
            models,
            response_role,
            *,
            online_renderer,
            request_logger,
            chat_template,
            chat_template_content_format,
        ):
            captured["online_renderer"] = online_renderer
            captured["request_logger"] = request_logger
            self.render_chat_request = MagicMock()

    import motor.engine_server.core.vllm.openai.serving_chat as serving_chat_mod

    monkeypatch.setattr(serving_chat_mod, "VllmOpenAIServingChat", FakeVllmOpenAIServingChat)
    monkeypatch.setattr(serving_chat_mod, "install_chat_render_validator", lambda serving: None)

    online_renderer = object()
    serving_chat_mod.OpenAIServingChat(
        engine_client=SimpleNamespace(),
        models=SimpleNamespace(),
        response_role="assistant",
        request_logger=None,
        chat_template=None,
        chat_template_content_format="auto",
        online_renderer=online_renderer,
    )

    assert captured["online_renderer"] is online_renderer


def test_serving_chat_forwards_openai_serving_render(serving_vllm_stubs, monkeypatch):
    captured: dict = {}

    class FakeVllmOpenAIServingChat:
        def __init__(
            self,
            engine_client,
            models,
            response_role,
            *,
            openai_serving_render,
            request_logger,
            chat_template,
            chat_template_content_format,
        ):
            captured["openai_serving_render"] = openai_serving_render
            self.render_chat_request = MagicMock()

    import motor.engine_server.core.vllm.openai.serving_chat as serving_chat_mod

    monkeypatch.setattr(serving_chat_mod, "VllmOpenAIServingChat", FakeVllmOpenAIServingChat)
    monkeypatch.setattr(serving_chat_mod, "install_chat_render_validator", lambda serving: None)

    render = object()
    serving_chat_mod.OpenAIServingChat(
        engine_client=SimpleNamespace(),
        models=SimpleNamespace(),
        response_role="assistant",
        request_logger=None,
        chat_template=None,
        chat_template_content_format="auto",
        openai_serving_render=render,
    )

    assert captured["openai_serving_render"] is render


def test_serving_completion_forwards_online_renderer(serving_vllm_stubs, monkeypatch):
    captured: dict = {}

    class FakeVllmOpenAIServingCompletion:
        def __init__(self, engine_client, models, *, online_renderer, request_logger):
            captured["online_renderer"] = online_renderer
            self.render_completion_request = MagicMock()

    import motor.engine_server.core.vllm.openai.serving_completion as serving_completion_mod

    monkeypatch.setattr(serving_completion_mod, "VllmOpenAIServingCompletion", FakeVllmOpenAIServingCompletion)
    monkeypatch.setattr(serving_completion_mod, "install_completion_render_validator", lambda serving: None)

    online_renderer = object()
    serving_completion_mod.OpenAIServingCompletion(
        engine_client=SimpleNamespace(),
        models=SimpleNamespace(),
        request_logger=None,
        online_renderer=online_renderer,
    )

    assert captured["online_renderer"] is online_renderer


def test_serving_chat_drops_wrong_era_render_kwarg(serving_vllm_stubs, monkeypatch):
    """When installed vLLM only accepts online_renderer, openai_serving_render must not be passed."""
    captured: dict = {}

    class FakeVllmOpenAIServingChat:
        def __init__(
            self,
            engine_client,
            models,
            response_role,
            *,
            online_renderer,
            request_logger,
            chat_template,
            chat_template_content_format,
        ):
            captured["online_renderer"] = online_renderer
            self.render_chat_request = MagicMock()

    import motor.engine_server.core.vllm.openai.serving_chat as serving_chat_mod

    monkeypatch.setattr(serving_chat_mod, "VllmOpenAIServingChat", FakeVllmOpenAIServingChat)
    monkeypatch.setattr(serving_chat_mod, "install_chat_render_validator", lambda serving: None)

    online = object()
    legacy = object()
    serving_chat_mod.OpenAIServingChat(
        engine_client=SimpleNamespace(),
        models=SimpleNamespace(),
        response_role="assistant",
        request_logger=None,
        chat_template=None,
        chat_template_content_format="auto",
        online_renderer=online,
        openai_serving_render=legacy,
    )

    assert captured["online_renderer"] is online
