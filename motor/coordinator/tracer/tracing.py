# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import dataclasses
import threading
import time
from collections.abc import Mapping
from typing import Optional
from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

# OpenTelemetry Related Introduction
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.sdk.environment_variables import OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import NoOpTracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.util import types

from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig

logger = get_logger(__name__)


class TracerManager(ThreadSafeSingleton):
    """Tracer Manager class, Singleton class"""

    _INSTRUMENTING_MODULE_NAME = "mindie.motor"
    _PROTOCOL_GRPC = "grpc"
    _PROTOCOL_HTTP = "http/protobuf"
    _TRACE_HEADERS = ("traceparent", "tracestate")
    _TEXTMAPPROPOGATOR = TraceContextTextMapPropagator()

    def __init__(self, config: CoordinatorConfig | None = None):
        """TracerManager init (runs once per process for the singleton)."""
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.config_lock = threading.RLock()

        if config is None:
            config = CoordinatorConfig()

        self.endpoint = config.tracer_config.endpoint
        self.root_sampling_rate = config.tracer_config.root_sampling_rate
        self.remote_parent_sampled = config.tracer_config.remote_parent_sampled
        self.remote_parent_not_sampled = config.tracer_config.remote_parent_not_sampled
        self.local_parent_sampled = config.tracer_config.local_parent_sampled
        self.local_parent_not_sampled = config.tracer_config.local_parent_not_sampled

        self._protocol = self.get_protocol()
        enable = len(self._protocol) > 0
        self._trace_provider = None  # set in init_tracer for shutdown()
        self.tracer = self.init_tracer(enable)
        logger.info(f"TracerManager init.(enable:{enable},endpoint:{self.endpoint},protocol:{self._protocol})")

    def update_config(self, config: CoordinatorConfig) -> None:
        """Update configuration for the TracerManager"""
        with self.config_lock:
            if self._trace_provider is not None:
                try:
                    self._trace_provider.shutdown()
                except Exception as e:
                    logger.warning("TracerManager: shutdown previous provider on config update failed: %s", e)
            self.endpoint = config.tracer_config.endpoint
            self.root_sampling_rate = config.tracer_config.root_sampling_rate
            self.remote_parent_sampled = config.tracer_config.remote_parent_sampled
            self.remote_parent_not_sampled = config.tracer_config.remote_parent_not_sampled
            self.local_parent_sampled = config.tracer_config.local_parent_sampled
            self.local_parent_not_sampled = config.tracer_config.local_parent_not_sampled

            self._protocol = self.get_protocol()
            enable = len(self._protocol) > 0
            self.tracer = self.init_tracer(enable)
            logger.info(f"TracerManager update.(enable:{enable},endpoint:{self.endpoint},protocol:{self._protocol})")

    def shutdown(self) -> None:
        """Flush and shutdown the TracerProvider so no spans are lost on process exit."""
        provider = getattr(self, "_trace_provider", None)
        if provider is None:
            return
        try:
            if hasattr(provider, "shutdown"):
                provider.shutdown()
            logger.debug("TracerManager: TracerProvider shutdown completed")
        except Exception as e:
            logger.warning("TracerManager: shutdown failed: %s", e)
        finally:
            self._trace_provider = None

    def init_tracer(self, enabled=True) -> trace_api.Tracer:
        """Obtain a configured Tracer instance"""
        if enabled:
            sampler = ParentBased(
                root=TraceIdRatioBased(self.root_sampling_rate),
                remote_parent_sampled=TraceIdRatioBased(self.remote_parent_sampled),
                remote_parent_not_sampled=TraceIdRatioBased(self.remote_parent_not_sampled),
                local_parent_sampled=TraceIdRatioBased(self.local_parent_sampled),
                local_parent_not_sampled=TraceIdRatioBased(self.local_parent_not_sampled),
            )

            span_exporter = self.get_span_exporter()

            trace_provider = TracerProvider(sampler=sampler)
            trace_provider.add_span_processor(BatchSpanProcessor(span_exporter))
            self._trace_provider = trace_provider
        else:
            self._trace_provider = None
            trace_provider = NoOpTracerProvider()

        trace_api.set_tracer_provider(trace_provider)
        return trace_api.get_tracer(self._INSTRUMENTING_MODULE_NAME)

    def get_span_exporter(self) -> SpanExporter:
        """Obtain the appropriate span exporter based on environment variables"""
        if self._protocol == self._PROTOCOL_GRPC:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        elif self._protocol == self._PROTOCOL_HTTP:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,  # type: ignore
            )
        else:
            raise ValueError(f"env OTEL_EXPORTER_OTLP_TRACES_PROTOCOL:'{self._protocol}' is invalid")

        return OTLPSpanExporter(endpoint=self.endpoint)

    def get_protocol(self) -> str:
        """Return OTLP protocol (grpc or http/protobuf) when endpoint is set; otherwise disable tracing.
        No strict URL regex: endpoint may be hostname, IPv4, IPv6, localhost, or https.
        """
        protocol = os.environ.get(OTEL_EXPORTER_OTLP_TRACES_PROTOCOL, self._PROTOCOL_GRPC)
        if protocol not in (self._PROTOCOL_GRPC, self._PROTOCOL_HTTP):
            logger.info("check: OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=%s invalid, tracing disabled", protocol)
            return ""
        if (self.endpoint or "").strip():
            return protocol
        logger.debug("check: endpoint empty, tracing disabled")
        return ""

    def extract_trace_context(self, headers: Mapping[str, str] | None) -> context_api.Context:
        """Extract the trace context from headers"""
        headers = headers or {}
        tmp_headers = {h: headers[h] for h in self._TRACE_HEADERS if h in headers}

        return self._TEXTMAPPROPOGATOR.extract(tmp_headers)

    def inject_trace_context(self) -> Mapping[str, str]:
        """Inject the current trace context into headers"""
        headers = {}
        self._TEXTMAPPROPOGATOR.inject(headers)

        return headers

    def contains_trace_headers(self, headers: Mapping[str, str]) -> bool:
        """Check whether the headers contain the trace header"""
        return any(h in headers for h in self._TRACE_HEADERS)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class TraceObj:
    """
    The trace object for distributed tracing, which includes the Span and trace header information.
    Attributes:
        span: The Span object currently being traced.
        meta_span: The Span object currently being traced.
        trace_headers: A dictionary of trace header information.
        meta_trace_headers: A dictionary of trace header information.
    """

    time_start: int = 0
    time_first_token: int = 0
    count_token: int = 0
    error_message: str = ""
    meta_error_message: str = ""

    parent_context: Optional[context_api.Context] = None
    span: Optional[trace_api.Span] = None
    meta_span: Optional[trace_api.Span] = None
    trace_headers: Mapping[str, str] = dataclasses.field(default_factory=dict)
    meta_trace_headers: Mapping[str, str] = dataclasses.field(default_factory=dict)

    def set_time_start(self) -> None:
        """
        Sets an attribute on the trace span if the trace object and span are available.
        """
        self.time_start = time.time_ns()

    def set_time_first_token(self) -> None:
        """
        Sets an attribute on the trace span if the trace object and span are available.
        """
        self.time_first_token = time.time_ns()
        self.add_trace_event("Received the first token")

    def set_count_token(self, cnt: int) -> None:
        """
        Sets an attribute on the trace span if the trace object and span are available.
        """
        self.count_token = cnt

    def set_end_and_ttft_tpot(self) -> str:
        """
        Sets an attribute on the trace span if the trace object and span are available.
        TTFT is only computed when time_first_token was set (e.g. first token received).
        TTOT (time to output token) is per-token average; when count_token is 0, TTOT is not computed.
        """
        time_end = time.time_ns()
        if self.time_first_token > 0:
            ttft = (self.time_first_token - self.time_start) // 1_000_000
            ttft_str = f"{ttft}"
        else:
            ttft_str = "N/A"
        if self.count_token > 0:
            ttot = (time_end - self.time_first_token) / (self.count_token * 1_000_000)
            ttot_str = f"{ttot}"
        else:
            ttot_str = "N/A"
        self.set_trace_attribute("TTFT(ms)", ttft_str)
        self.set_trace_attribute("TTOT(ms)", ttot_str)
        self.set_trace_attribute("TOKEN_COUNT", f"{self.count_token}")
        return f"Tracer: TTFT: {ttft_str}ms, TTOT: {ttot_str}ms, count_token: {self.count_token}"

    def set_trace_error_message(self, error_log: str, is_meta: bool = False) -> str:
        """
        Sets a readable error message on the trace span and returns the formatted log.
        """
        raw_error_log = str(error_log)
        formatted_error_message = f"error message：{raw_error_log}"
        if is_meta:
            self.meta_error_message = raw_error_log
        else:
            self.error_message = raw_error_log
        self.set_trace_attribute("error.message", formatted_error_message, is_meta)
        self.add_trace_event("Error message", attributes={"error.message": formatted_error_message}, is_meta=is_meta)
        return formatted_error_message

    def set_trace_attribute(self, key: str, value: types.AttributeValue, is_meta: bool = False) -> None:
        """
        Sets an attribute on the trace span if the trace object and span are available.
        """
        tmp_span = self.meta_span if is_meta else self.span
        if tmp_span is None:
            return
        tmp_span.set_attribute(key, value)

    def add_trace_event(
        self, name: str, attributes: types.Attributes = None, timestamp: Optional[int] = None, is_meta: bool = False
    ) -> None:
        """
        Adds an event to the trace span if the trace object and span are available.
        """
        tmp_span = self.meta_span if is_meta else self.span
        if tmp_span is None:
            return
        tmp_span.add_event(name, attributes, timestamp)

    def get_trace_headers_dict(self, is_meta: bool = False) -> dict[str, str]:
        """
        Returns a copy of the trace headers as a dict.
        Returns an empty dict if trace_headers (or meta_trace_headers when is_meta) is not set.
        """
        tmp_trace_headers = self.meta_trace_headers if is_meta else self.trace_headers
        if tmp_trace_headers is None:
            return {}
        return dict(tmp_trace_headers)

    def set_trace_exception(self, exception: BaseException, is_meta: bool = False) -> None:
        """
        Records an exception into the current trace span.
        If is_meta is True, the exception is recorded into meta_span; otherwise, it is recorded into span.
        Args:
        exception (BaseException): The exception object to be recorded.
        is_meta (bool, optional): Whether to use meta_span. Default is False.
        """
        tmp_span = self.meta_span if is_meta else self.span
        if tmp_span is None:
            return
        tmp_span.record_exception(exception)

    def set_trace_status(self, exception: BaseException, is_meta: bool = False) -> None:
        """
        Set the status of the current span to ERROR, with the exception information included.
        If is_meta is True, set it to meta_span; otherwise, set it to span.
        Args:
        exception (BaseException): The exception object used to generate the status description.
        is_meta (bool, optional): Whether to use meta_span. Default is False.
        """
        tmp_span = self.meta_span if is_meta else self.span
        if tmp_span is None:
            return
        tmp_span.set_status(
            Status(
                status_code=StatusCode.ERROR,
                description=f"{type(exception).__name__}: {exception}",
            )
        )
