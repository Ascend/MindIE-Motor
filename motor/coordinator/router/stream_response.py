# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field

import httpx
from anyio import CancelScope
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from motor.common.http.security_utils import sanitize_error_message
import motor.common.utils.error as cancel_error
from motor.coordinator.router.anthropic_envelope import (
    anthropic_error_response,
    anthropic_stream_error_frame,
    detail_to_message,
    is_anthropic_error_body,
)
from motor.coordinator.router.upstream_error import (
    UpstreamHTTPError,
    render_transport_error,
    render_upstream_error,
)


@dataclass
class StreamCommitController:
    """Coordinate the upstream acceptance checks required before HTTP 200."""

    required_parts: frozenset[str]
    _attempt_id: int = 0
    _ready_parts: set[str] = field(default_factory=set)
    _ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    _committed_event: asyncio.Event = field(default_factory=asyncio.Event)
    _commit_started: bool = False
    _committed: bool = False

    @classmethod
    def requiring(cls, parts: Iterable[str]) -> "StreamCommitController":
        required = frozenset(parts)
        if not required:
            raise ValueError("Stream commit controller requires at least one readiness part")
        return cls(required_parts=required)

    def begin_attempt(self, attempt_id: int) -> None:
        if self.commit_sealed:
            raise RuntimeError("Cannot start a new stream attempt after the response commit boundary")
        self._attempt_id = attempt_id
        self._ready_parts.clear()
        self._ready_event.clear()
        self._committed_event.clear()

    def mark_ready(self, part: str, attempt_id: int) -> None:
        if attempt_id != self._attempt_id or self.commit_sealed:
            return
        if part not in self.required_parts:
            raise ValueError(f"Unexpected stream readiness part: {part}")
        self._ready_parts.add(part)
        if self.required_parts.issubset(self._ready_parts):
            self._ready_event.set()

    async def wait_ready(self) -> None:
        await self._ready_event.wait()

    async def wait_committed(self) -> None:
        await self._committed_event.wait()

    @property
    def ready_to_commit(self) -> bool:
        return self._ready_event.is_set()

    @property
    def commit_sealed(self) -> bool:
        return self.ready_to_commit or self._commit_started or self._committed

    @property
    def committed(self) -> bool:
        return self._committed

    def mark_commit_started(self) -> None:
        self._commit_started = True

    def mark_committed(self) -> None:
        self._commit_started = True
        self._committed = True
        self._committed_event.set()


class CommitAwareStreamingResponse(Response):
    """Delay HTTP 200 until the router confirms all upstream legs accepted."""

    media_type = "text/event-stream"

    def __init__(
        self,
        content: AsyncIterator[bytes | str],
        controller: StreamCommitController,
        *,
        status_code: int = status.HTTP_200_OK,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
        on_first_body_sent: Callable[[], None] | None = None,
        anthropic_entry: bool = False,
    ) -> None:
        super().__init__(
            content=None,
            status_code=status_code,
            headers=headers,
            media_type=media_type or self.media_type,
            background=background,
        )
        self.raw_headers = [(name, value) for name, value in self.raw_headers if name != b"content-length"]
        self._raw_iterator = content
        self.controller = controller
        self._on_first_body_sent = on_first_body_sent
        self._anthropic_entry = anthropic_entry
        self._first_body_sent = False
        self._finished = False
        self._consumer_mode: str | None = None
        # Kept for unit tests and internal callers that consume the response directly.
        # Direct iteration and ASGI serving are mutually exclusive and guarded when
        # either path starts consuming the shared raw iterator.
        self.body_iterator = self._compatibility_iterator()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self._claim_consumer("asgi")
        stream_task = asyncio.create_task(self._stream_response(scope, receive, send))
        disconnect_task = asyncio.create_task(self._listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait(
                (stream_task, disconnect_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stream_task in done:
                await stream_task
            else:
                await disconnect_task
                stream_task.cancel(msg=cancel_error.CLIENT_DISCONNECT)
                await asyncio.gather(stream_task, return_exceptions=True)
        finally:
            await self._cancel_and_wait(stream_task, disconnect_task)

        if self.background is not None:
            await self.background()

    async def _stream_response(self, scope: Scope, receive: Receive, send: Send) -> None:
        terminal: asyncio.Future[Exception | None] = asyncio.get_running_loop().create_future()
        pump_task = asyncio.create_task(self._pump_stream(send, terminal))
        ready_task = asyncio.create_task(self.controller.wait_ready())
        cancel_reason = None
        try:
            done, _ = await asyncio.wait(
                (terminal, ready_task),
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Readiness is the semantic commit boundary even if the stream task
            # finishes before the ASGI task gets scheduled to send HTTP 200.
            if terminal in done and not self.controller.ready_to_commit:
                error = terminal.result()
                if error is not None:
                    raise error
                raise RuntimeError("Streaming request ended before the commit barrier was satisfied")

            if not self.controller.ready_to_commit:
                await ready_task

            self.controller.mark_commit_started()
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            self.controller.mark_committed()
            await pump_task
        except OSError as error:  # pylint: disable=try-except-raise
            raise ClientDisconnect() from error
        except ClientDisconnect:  # pylint: disable=try-except-raise
            raise
        except asyncio.CancelledError as error:
            cancel_reason = error.args[0] if error.args else None
            raise
        except Exception as error:
            if self.controller.committed:
                await self._send_committed_error(send, error)
            else:
                await self._send_precommit_error(scope, receive, send, error)
        finally:
            await self._cancel_and_wait(pump_task, ready_task, reason=cancel_reason)

    async def _pump_stream(
        self,
        send: Send,
        terminal: asyncio.Future[Exception | None],
    ) -> None:
        try:
            async for item in self._raw_iterator:
                await self.controller.wait_committed()
                await self._send_body(send, item, more_body=True)
                self._mark_first_body_sent()
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except OSError as error:
            raise ClientDisconnect() from error
        except Exception as error:
            if self.controller.commit_sealed:
                await self.controller.wait_committed()
                await self._send_committed_error(send, error)
            elif not terminal.done():
                terminal.set_result(error)
        else:
            if self.controller.commit_sealed:
                await self.controller.wait_committed()
                await self._finish(send)
            elif not terminal.done():
                terminal.set_result(None)
        finally:
            with CancelScope(shield=True):
                await self._close_iterator()

    async def _send_precommit_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        error: Exception,
    ) -> None:
        # Order is intentional: the engine-specific UpstreamHTTPError / httpx.RequestError cases
        # are matched before the generic HTTPException / Exception fallbacks so an upstream failure
        # keeps its own status and body instead of collapsing into a generic 500. Always keep the
        # most specific types first if this chain is extended.
        if isinstance(error, UpstreamHTTPError):
            # Engine-supplied error bodies pass through verbatim for every protocol
            # (backend error pass-through); only coordinator-synthesized errors below
            # get a protocol-specific envelope.
            response = render_upstream_error(error)
        elif self._anthropic_entry:
            response = self._anthropic_precommit_response(error)
        elif isinstance(error, httpx.RequestError):
            response = render_transport_error(error)
        elif isinstance(error, HTTPException):
            response = JSONResponse(
                status_code=error.status_code,
                content={"detail": error.detail},
                headers=error.headers,
            )
        else:
            response = JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "error": {
                        "message": sanitize_error_message(str(error)),
                        "type": type(error).__name__,
                        "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    }
                },
            )
        try:
            await response(scope, receive, send)
        except OSError as send_error:
            raise ClientDisconnect() from send_error
        self._finished = True

    @staticmethod
    def _anthropic_precommit_response(error: Exception) -> JSONResponse:
        """Render a coordinator-synthesized pre-commit error in the Anthropic envelope."""
        if isinstance(error, httpx.TimeoutException):
            return anthropic_error_response(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                message=str(error),
            )
        if isinstance(error, httpx.RequestError):
            return anthropic_error_response(
                status_code=status.HTTP_502_BAD_GATEWAY,
                message=str(error),
            )
        if isinstance(error, HTTPException):
            return anthropic_error_response(
                status_code=error.status_code,
                message=detail_to_message(error.detail),
                headers=error.headers,
            )
        return anthropic_error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=str(error),
        )

    async def _send_committed_error(self, send: Send, error: Exception) -> None:
        if self._finished:
            return
        chunk = self._committed_error_chunk(error)
        try:
            await self._send_body(send, chunk, more_body=True)
            await self._finish(send)
        except OSError as send_error:
            raise ClientDisconnect() from send_error

    def _committed_error_chunk(self, error: Exception) -> bytes:
        if self._anthropic_entry:
            return self._anthropic_committed_error_chunk(error)

        if isinstance(error, UpstreamHTTPError) and error.body:
            try:
                payload = json.loads(error.body)
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                return b"data: " + encoded + b"\n\n"
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        code = self._committed_error_status_code(error)
        # Match the pre-commit / non-stream error envelope ({"error": {...}}) so clients can
        # parse a synthesized mid-stream error the same way regardless of when it occurred.
        # UpstreamHTTPError bodies are still forwarded verbatim above (the engine supplies its
        # own envelope); only Coordinator-synthesized errors are wrapped here.
        payload = {
            "error": {
                "message": sanitize_error_message(str(error)),
                "type": type(error).__name__,
                "code": code,
            }
        }
        return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()

    def _anthropic_committed_error_chunk(self, error: Exception) -> bytes:
        """One Anthropic ``event: error`` frame for a committed stream failure.

        Anthropic streams have no ``[DONE]`` sentinel and no OpenAI-style
        ``data: {"error": ...}`` frame; exactly one error frame terminates the stream.
        An engine-supplied Anthropic envelope is forwarded verbatim inside the frame.
        """
        if isinstance(error, UpstreamHTTPError) and error.body:
            envelope = is_anthropic_error_body(error.body)
            if envelope is not None:
                encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
                return b"event: error\ndata: " + encoded + b"\n\n"
        return anthropic_stream_error_frame(
            status_code=self._committed_error_status_code(error),
            message=str(error),
        )

    @staticmethod
    def _committed_error_status_code(error: Exception) -> int:
        if isinstance(error, UpstreamHTTPError):
            return error.status_code
        if isinstance(error, httpx.TimeoutException):
            return status.HTTP_504_GATEWAY_TIMEOUT
        if isinstance(error, httpx.RequestError):
            return status.HTTP_502_BAD_GATEWAY
        if isinstance(error, HTTPException):
            return error.status_code
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code
        return status.HTTP_500_INTERNAL_SERVER_ERROR

    async def _finish(self, send: Send) -> None:
        if self._finished:
            return
        self._finished = True
        await self._send_body(send, b"", more_body=False)

    async def _send_body(self, send: Send, item: bytes | memoryview | str, *, more_body: bool) -> None:
        body = item if isinstance(item, bytes | memoryview) else item.encode(self.charset)
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": more_body,
            }
        )

    async def _compatibility_iterator(self) -> AsyncGenerator[bytes | str, None]:
        self._claim_consumer("body_iterator")
        try:
            async for item in self._raw_iterator:
                if not self.controller.committed:
                    self.controller.mark_commit_started()
                    self.controller.mark_committed()
                self._mark_first_body_sent()
                yield item
        except Exception as error:
            yield self._committed_error_chunk(error).decode()
        finally:
            await self._close_iterator()

    def _claim_consumer(self, mode: str) -> None:
        if self._consumer_mode is not None:
            raise RuntimeError(
                "CommitAwareStreamingResponse is single-consumer: "
                f"{self._consumer_mode} already claimed the stream; cannot start {mode}"
            )
        self._consumer_mode = mode

    async def _close_iterator(self) -> None:
        close = getattr(self._raw_iterator, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()

    def _mark_first_body_sent(self) -> None:
        if self._first_body_sent:
            return
        self._first_body_sent = True
        if self._on_first_body_sent is not None:
            self._on_first_body_sent()

    @staticmethod
    async def _listen_for_disconnect(receive: Receive) -> None:
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return

    @staticmethod
    async def _cancel_and_wait(*tasks: asyncio.Task, reason: str | None = None) -> None:
        for task in tasks:
            if not task.done():
                if reason is None:
                    task.cancel()
                else:
                    task.cancel(msg=reason)
        await asyncio.gather(*tasks, return_exceptions=True)
