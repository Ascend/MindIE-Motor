# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Response format adapters (OpenAI Completion <-> Chat)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

__all__ = [
    "adapt_completion_nonstream_to_chat",
    "adapt_completion_stream_chunk_to_chat",
    "is_completion_like_stream_chunk",
    "encode_stream_chunk_bytes",
    "parse_stream_chunk_json",
    "strip_nonstream_response_body_for_client",
    "strip_stream_chunk_bytes_for_client",
]


_COMPLETION_EXPORTS = {
    "adapt_completion_nonstream_to_chat",
    "adapt_completion_stream_chunk_to_chat",
    "is_completion_like_stream_chunk",
}
_STREAM_EXPORTS = {
    "encode_stream_chunk_bytes",
    "parse_stream_chunk_json",
    "strip_nonstream_response_body_for_client",
    "strip_stream_chunk_bytes_for_client",
}


def __getattr__(name: str):
    """Resolve adapter exports lazily to keep protocol-only imports dependency-free."""
    if name in _COMPLETION_EXPORTS:
        from motor.coordinator.router.adapters import completion_to_chat

        return getattr(completion_to_chat, name)
    if name in _STREAM_EXPORTS:
        from motor.coordinator.router.adapters import stream

        return getattr(stream, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
