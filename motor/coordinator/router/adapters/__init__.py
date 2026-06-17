# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Response format adapters (OpenAI Completion <-> Chat)."""

__all__ = [
    "adapt_completion_nonstream_to_chat",
    "adapt_completion_stream_chunk_to_chat",
    "is_completion_like_stream_chunk",
    "encode_stream_chunk_bytes",
    "parse_stream_chunk_json",
    "strip_nonstream_response_body_for_client",
    "strip_stream_chunk_bytes_for_client",
]

from motor.coordinator.router.adapters.completion_to_chat import (
    adapt_completion_nonstream_to_chat,
    adapt_completion_stream_chunk_to_chat,
    is_completion_like_stream_chunk,
)

from motor.coordinator.router.adapters.stream import (
    encode_stream_chunk_bytes,
    parse_stream_chunk_json,
    strip_nonstream_response_body_for_client,
    strip_stream_chunk_bytes_for_client,
)
