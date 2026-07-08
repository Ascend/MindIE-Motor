from unittest.mock import patch, MagicMock
import json
from pydantic.dataclasses import dataclass

from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.trace import Span, StatusCode
from opentelemetry.sdk.environment_variables import OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
import pytest

from motor.coordinator.tracer.tracing import TracerManager, TraceObj

HTTP_CONFIG = "http://127.0.0.1:4318/v1/traces"
HTTP_ENV = "http/protobuf"
GRPC_CONFIG = "grpc://127.0.0.1:4317"
GRPC_ENV = "grpc"
INVLID_ENV = "invalid"


@dataclass
class TracerConfig:
    endpoint: str
    root_sampling_rate: float = 1.0
    remote_parent_sampled: float = 1.0
    remote_parent_not_sampled: float = 0.0
    local_parent_sampled: float = 1.0
    local_parent_not_sampled: float = 0.0


@dataclass
class ConfigWithTracer:
    tracer_config: TracerConfig


@patch.dict('os.environ', {OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: HTTP_ENV})
@patch("motor.config.coordinator.CoordinatorConfig")
def test_tracer_manager_update_config(mock_coordinator_config):
    mock_coordinator_config.return_value = ConfigWithTracer(tracer_config=TracerConfig(endpoint=HTTP_CONFIG))

    tm = TracerManager()
    tm.update_config(mock_coordinator_config())

    assert tm._protocol == HTTP_ENV
    assert tm.endpoint == HTTP_CONFIG
    assert tm.tracer is not None
    assert isinstance(tm.tracer, trace_api.Tracer)


@patch.dict('os.environ', {OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: INVLID_ENV})
def test_tracer_manager_get_protocol_invalid():
    tm = TracerManager()
    tm.endpoint = HTTP_CONFIG
    result = tm.get_protocol()

    assert result == ""


def test_get_span_exporter_grpc():
    with patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"):
        tm = TracerManager()
        tm._protocol = GRPC_ENV
        exporter = tm.get_span_exporter()
        assert isinstance(exporter, MagicMock)


def test_get_span_exporter_http_protobuf():
    with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"):
        tm = TracerManager()
        tm._protocol = HTTP_ENV
        exporter = tm.get_span_exporter()
        assert isinstance(exporter, MagicMock)


def test_get_span_exporter_invalid_protocol():
    with pytest.raises(ValueError):
        tm = TracerManager()
        tm._protocol = INVLID_ENV
        tm.get_span_exporter()


def test_tracer_manager_extract_trace_context():
    tm = TracerManager()
    headers = {"traceparent": "00-0af7651916cd43dd8448ebd08f9ca98e-0100000000000000-01", "tracestate": "foo=bar"}
    context = tm.extract_trace_context(headers)
    assert isinstance(context, Context)


def test_tracer_manager_contains_trace_headers():
    tm = TracerManager()
    headers = {"traceparent": "00-0af7651916cd43dd8448ebd08f9ca98e-0100000000000000-01"}
    assert tm.contains_trace_headers(headers) is True
    assert tm.contains_trace_headers({}) is False


def test_trace_obj_set_trace_attribute():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    trace_obj.set_trace_attribute("key", "value")
    span.set_attribute.assert_called_once_with("key", "value")


def test_trace_obj_set_trace_prompt_records_structure_without_content():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)

    trace_obj.set_trace_prompt(
        {
            "model": "qwen",
            "messages": [{"role": "user", "content": "plain secret request"}],
            "max_tokens": 8,
        }
    )

    span.set_attribute.assert_called_once()
    key, value = span.set_attribute.call_args.args
    structure = json.loads(value)
    assert key == "request.structure"
    assert "plain secret request" not in value
    assert "qwen" not in value
    assert structure["fields"]["messages"]["items"][0]["fields"]["content"] == {"type": "str", "length": 20}


def test_trace_obj_add_trace_event():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    trace_obj.add_trace_event("event_name", {"attr": "value"}, 123456789)
    span.add_event.assert_called_once_with("event_name", {"attr": "value"}, 123456789)


def test_trace_obj_get_trace_headers_dict():
    trace_obj = TraceObj()
    headers = {"traceparent": "0af7651916cd43dd8448ebd08f9ca98e"}
    trace_obj.trace_headers = headers
    assert trace_obj.get_trace_headers_dict() == headers


def test_trace_obj_set_trace_exception():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    exc = Exception("test")
    trace_obj.set_trace_exception(exc)
    span.record_exception.assert_called_once_with(exc)


# ============ Test extract_trace_context ============
def test_extract_trace_context_with_headers():
    headers = {"traceparent": "123", "tracestate": "456"}
    context = TracerManager().extract_trace_context(headers)
    assert context is not None


# ============ Test contains_trace_headers ============
def test_contains_trace_headers_true():
    headers = {"traceparent": "123", "tracestate": "456"}
    assert TracerManager().contains_trace_headers(headers) is True


def test_contains_trace_headers_false():
    headers = {"other_header": "value"}
    assert TracerManager().contains_trace_headers(headers) is False


# ============ Test the methods of the TraceObj class ============
def test_traceobj_set_trace_attribute():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    trace_obj.set_trace_attribute("key", "value")
    span.set_attribute.assert_called_once_with("key", "value")


def test_traceobj_add_trace_event():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    trace_obj.add_trace_event("event_name", {"attr": "value"}, 123456789)
    span.add_event.assert_called_once_with("event_name", {"attr": "value"}, 123456789)


def test_traceobj_get_trace_headers_dict():
    headers = {"traceparent": "123", "tracestate": "456"}
    trace_obj = TraceObj(trace_headers=headers)
    result = trace_obj.get_trace_headers_dict()
    assert result == headers


def test_traceobj_set_trace_exception():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    exception = Exception("Test exception")
    trace_obj.set_trace_exception(exception)
    span.record_exception.assert_called_once_with(exception)


def test_traceobj_set_trace_status():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    exception = Exception("Test exception")
    trace_obj.set_trace_status(exception)

    actual_status = span.set_status.call_args[0][0]
    assert actual_status.status_code == StatusCode.ERROR
    assert actual_status.description == "Exception: Test exception"


def test_traceobj_set_trace_status_with_meta_span():
    span = MagicMock(Span)
    meta_span = MagicMock(Span)
    trace_obj = TraceObj(span=span, meta_span=meta_span)
    exception = Exception("Test exception")
    trace_obj.set_trace_status(exception, is_meta=True)

    actual_status = meta_span.set_status.call_args[0][0]
    assert actual_status.status_code == StatusCode.ERROR
    assert actual_status.description == "Exception: Test exception"


def test_traceobj_set_trace_attribute_with_meta_span():
    span = MagicMock(Span)
    meta_span = MagicMock(Span)
    trace_obj = TraceObj(span=span, meta_span=meta_span)
    trace_obj.set_trace_attribute("key", "value", is_meta=True)
    meta_span.set_attribute.assert_called_once_with("key", "value")


def test_traceobj_add_trace_event_with_meta_span():
    span = MagicMock(Span)
    meta_span = MagicMock(Span)
    trace_obj = TraceObj(span=span, meta_span=meta_span)
    trace_obj.add_trace_event("event_name", {"attr": "value"}, 123456789, is_meta=True)
    meta_span.add_event.assert_called_once_with("event_name", {"attr": "value"}, 123456789)


def test_traceobj_get_trace_headers_dict_with_meta():
    headers = {"traceparent": "123", "tracestate": "456"}
    trace_obj = TraceObj(trace_headers=headers, meta_trace_headers=headers)
    result = trace_obj.get_trace_headers_dict(is_meta=True)
    assert result == headers


def test_traceobj_get_trace_headers_dict_with_empty():
    trace_obj = TraceObj()
    result = trace_obj.get_trace_headers_dict()
    assert result == {}


def test_trace_obj_set_trace_status():
    span = MagicMock(Span)
    trace_obj = TraceObj(span=span)
    exc = Exception("test")
    trace_obj.set_trace_status(exc)
    span.set_status.assert_called_once()
    args, _ = span.set_status.call_args
    status = args[0]
    assert status.status_code == StatusCode.ERROR
    assert "Exception: test" in status.description
