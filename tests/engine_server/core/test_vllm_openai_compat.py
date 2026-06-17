from types import SimpleNamespace

import pytest

from motor.engine_server.core.vllm.vllm_openai_compat import (
    build_openai_serving_render_kwargs,
    kwargs_matching_signature,
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
