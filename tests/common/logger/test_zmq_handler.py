# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import logging
import time
from unittest.mock import MagicMock, patch

import pytest
import zmq

from motor.common.logger.zmq_handler import ZmqPushHandler

COLLECTOR_ADDRESS = "tcp://127.0.0.1:15555"
LOGGER_NAME = "motor.logger"


@pytest.fixture
def mock_zmq(monkeypatch):
    monkeypatch.setenv("MOTOR_LOG_COLLECTOR_ADDRESS", COLLECTOR_ADDRESS)
    with patch("zmq.Context") as ctx:
        yield ctx


@pytest.fixture
def mock_zmq_no_address(monkeypatch):
    monkeypatch.delenv("MOTOR_LOG_COLLECTOR_ADDRESS", raising=False)
    with patch("zmq.Context") as ctx:
        yield ctx


def make_record(msg, *args):
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=args or None, exc_info=None
    )


def connected_handler(mock_zmq):
    handler = ZmqPushHandler()
    return handler, mock_zmq.instance.return_value.socket.return_value


def disconnected_handler(mock_zmq):
    mock_zmq.instance.return_value.socket.side_effect = zmq.ZMQError(zmq.ETERM)
    return ZmqPushHandler()


def warnings_in(caplog, keyword):
    return [r for r in caplog.records if keyword in r.getMessage()]


# 1) Initialization: no socket is created when the address env var is absent
def test_init_without_address_env_stays_disconnected(mock_zmq_no_address):
    handler = ZmqPushHandler()
    mock_zmq_no_address.instance.assert_not_called()
    assert handler._socket is None
    assert handler._connected is False
    assert handler._address is None


# 1) Initialization: create a PUSH socket, set HWM, and connect to the env-provided address
def test_init_creates_push_socket_and_connects_to_env_address(mock_zmq):
    handler = ZmqPushHandler()
    ctx = mock_zmq.instance.return_value
    ctx.socket.assert_called_once_with(zmq.PUSH)
    socket = ctx.socket.return_value
    socket.set_hwm.assert_called_once()
    socket.connect.assert_called_once_with(COLLECTOR_ADDRESS)
    assert handler._socket is socket
    assert handler._connected is True
    assert handler._address == COLLECTOR_ADDRESS


# 3) Any ZMQ error during initialization degrades gracefully without raising and keeps the handler disconnected
@pytest.mark.parametrize("stage", ["create", "set_hwm", "connect"])
def test_init_swallows_zmq_error_at_each_stage(mock_zmq, stage):
    ctx = mock_zmq.instance.return_value
    if stage == "create":
        ctx.socket.side_effect = zmq.ZMQError(zmq.ETERM)
    else:
        getattr(ctx.socket.return_value, stage).side_effect = zmq.ZMQError(zmq.ETERM)
    handler = ZmqPushHandler()
    assert handler._connected is False
    assert handler._address == COLLECTOR_ADDRESS
    if stage == "create":
        assert handler._socket is None


# 2) emit: sends formatted UTF-8 bytes (expected values are literal bytes, independent of encoder implementation)
@pytest.mark.parametrize(
    "message, expected_bytes",
    [
        ("plain ascii message", b"prefix plain ascii message"),
        ("中文日志消息", b"prefix \xe4\xb8\xad\xe6\x96\x87\xe6\x97\xa5\xe5\xbf\x97\xe6\xb6\x88\xe6\x81\xaf"),
    ],
)
def test_emit_sends_utf8_encoded_formatted_message(message, expected_bytes, mock_zmq):
    handler, socket = connected_handler(mock_zmq)
    handler.emit(make_record("prefix %s", message))
    socket.send.assert_called_once_with(expected_bytes, zmq.NOBLOCK)


# 3) emit: when disconnected and reconnect fails, drop the line, warn only once, and never raise
def test_emit_drops_line_and_warns_once_when_collector_unreachable(mock_zmq, caplog):
    handler = disconnected_handler(mock_zmq)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        handler.emit(make_record("first dropped"))
        handler.emit(make_record("second dropped"))
    assert len(warnings_in(caplog, "collector unreachable")) == 1
    assert handler._warned is True


# 3) emit: with no configured address, drop the line and warn only once
def test_emit_without_address_drops_line_and_warns_once(mock_zmq_no_address, caplog):
    handler = ZmqPushHandler()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        handler.emit(make_record("no collector"))
        handler.emit(make_record("still no collector"))
    assert len(warnings_in(caplog, "collector unreachable")) == 1


# Reconnect throttling: skip repeated attempts within the interval
def test_reconnect_is_throttled_within_interval(mock_zmq):
    handler, _ = connected_handler(mock_zmq)
    handler._connected = False
    handler._last_reconnect_attempt = time.monotonic()
    with patch.object(handler, "_connect") as mock_connect:
        assert handler._try_reconnect() is False
    mock_connect.assert_not_called()


# Reconnect success: close the old socket without sending, send on the new socket, and reset the one-shot warning
def test_reconnect_after_interval_closes_old_socket_and_sends_on_new(mock_zmq):
    factory = mock_zmq.instance.return_value.socket
    old_socket = MagicMock(name="old_socket")
    new_socket = MagicMock(name="new_socket")
    factory.side_effect = [old_socket, new_socket]
    handler = ZmqPushHandler()
    handler._connected = False
    handler._warned = True
    handler._last_reconnect_attempt = time.monotonic() - ZmqPushHandler._RECONNECT_INTERVAL - 1
    handler.emit(make_record("after reconnect"))
    assert factory.call_count == 2
    old_socket.close.assert_called_once_with(linger=0)
    old_socket.send.assert_not_called()
    new_socket.send.assert_called_once_with(b"after reconnect", zmq.NOBLOCK)
    new_socket.close.assert_not_called()
    assert handler._socket is new_socket
    assert handler._connected is True
    assert handler._warned is False


# 3) emit: HWM full (EAGAIN) only counts the dropped line; do not raise or disconnect
def test_emit_swallows_hwm_eagain_without_raising(mock_zmq):
    handler, socket = connected_handler(mock_zmq)
    socket.send.side_effect = zmq.ZMQError(zmq.EAGAIN)
    handler._last_hwm_warn = time.monotonic()
    handler.emit(make_record("during backpressure"))
    assert handler._hwm_drops == 1
    assert handler._connected is True


# 3) HWM drops are reported once per interval: force the previous interval far in the past, warn once, then reset
def test_hwm_drops_reported_once_per_interval_then_reset(mock_zmq, caplog):
    handler, socket = connected_handler(mock_zmq)
    socket.send.side_effect = zmq.ZMQError(zmq.EAGAIN)
    handler._last_hwm_warn = time.monotonic() - ZmqPushHandler._HWM_WARN_INTERVAL - 1
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _ in range(3):
            handler.emit(make_record("backlogged"))
    assert len(warnings_in(caplog, "HWM full")) == 1
    assert handler._hwm_drops == 2


# 3) emit: non-EAGAIN send failure marks the handler disconnected, warns only once, and never raises
def test_emit_swallows_send_failure_marks_disconnected_and_warns_once(mock_zmq, caplog):
    handler, socket = connected_handler(mock_zmq)
    socket.send.side_effect = zmq.ZMQError(zmq.ETERM)
    handler._last_reconnect_attempt = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        handler.emit(make_record("lost line"))
    assert handler._connected is False
    assert len(warnings_in(caplog, "send to")) == 1


# 3) emit: reconnect immediately after a send failure when throttling has expired; close the old socket and recover without resending
def test_send_failure_triggers_immediate_reconnect(mock_zmq):
    factory = mock_zmq.instance.return_value.socket
    old_socket = MagicMock(name="old_socket")
    new_socket = MagicMock(name="new_socket")
    factory.side_effect = [old_socket, new_socket]
    handler = ZmqPushHandler()
    handler._last_reconnect_attempt = time.monotonic() - ZmqPushHandler._RECONNECT_INTERVAL - 1
    old_socket.send.side_effect = zmq.ZMQError(zmq.ETERM)
    handler.emit(make_record("lost then reconnect"))
    old_socket.close.assert_called_once_with(linger=0)
    old_socket.send.assert_called_once()
    new_socket.send.assert_not_called()
    assert factory.call_count == 2
    assert handler._socket is new_socket
    assert handler._connected is True


# 3) Reconnect swallows ZMQError from closing the old socket and continues reconnecting
def test_reconnect_swallows_close_error_of_old_socket(mock_zmq):
    factory = mock_zmq.instance.return_value.socket
    old_socket = MagicMock(name="old_socket")
    new_socket = MagicMock(name="new_socket")
    old_socket.close.side_effect = zmq.ZMQError(zmq.ETERM)
    factory.side_effect = [old_socket, new_socket]
    handler = ZmqPushHandler()
    handler._connected = False
    handler._last_reconnect_attempt = time.monotonic() - ZmqPushHandler._RECONNECT_INTERVAL - 1
    handler.emit(make_record("after close error"))
    old_socket.close.assert_called_once_with(linger=0)
    assert handler._socket is new_socket
    assert handler._connected is True


# 3) Do not warn again when a previous send failure already set _warned=True
def test_send_failure_with_warned_flag_does_not_warn_again(mock_zmq, caplog):
    handler, socket = connected_handler(mock_zmq)
    socket.send.side_effect = zmq.ZMQError(zmq.ETERM)
    handler._warned = True
    handler._last_reconnect_attempt = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        handler.emit(make_record("already warned"))
    assert handler._connected is False
    assert len(warnings_in(caplog, "send to")) == 0


# 3) Fallback send hitting EAGAIN across intervals aggregates the warning and resets the counter
def test_emit_fallback_hwm_warning_reported_after_interval(mock_zmq, caplog):
    handler, socket = connected_handler(mock_zmq)
    socket.send.side_effect = zmq.ZMQError(zmq.EAGAIN)
    handler._last_hwm_warn = time.monotonic() - ZmqPushHandler._HWM_WARN_INTERVAL - 1
    record = make_record("raw message text")
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with patch.object(handler, "format", side_effect=ValueError("bad formatter")):
            for _ in range(2):
                handler.emit(record)
    assert len(warnings_in(caplog, "HWM full (fallback path)")) == 1
    assert handler._hwm_drops == 1


# 3) Fallback send with a non-EAGAIN error marks the handler disconnected and reconnects immediately
def test_emit_fallback_send_failure_marks_disconnected_and_reconnects(mock_zmq):
    factory = mock_zmq.instance.return_value.socket
    old_socket = MagicMock(name="old_socket")
    new_socket = MagicMock(name="new_socket")
    factory.side_effect = [old_socket, new_socket]
    handler = ZmqPushHandler()
    handler._last_reconnect_attempt = time.monotonic() - ZmqPushHandler._RECONNECT_INTERVAL - 1
    old_socket.send.side_effect = zmq.ZMQError(zmq.ETERM)
    record = make_record("raw message text")
    with patch.object(handler, "format", side_effect=ValueError("bad formatter")):
        handler.emit(record)
    old_socket.close.assert_called_once_with(linger=0)
    assert handler._socket is new_socket
    assert handler._connected is True


# 2) emit: fall back to the raw message text when formatting fails
def test_emit_falls_back_to_raw_message_when_formatting_fails(mock_zmq):
    handler, socket = connected_handler(mock_zmq)
    record = make_record("raw message text")
    with patch.object(handler, "format", side_effect=ValueError("bad formatter")):
        handler.emit(record)
    socket.send.assert_called_once_with(b"raw message text", zmq.NOBLOCK)


# 3) emit: formatting failure and fallback HWM full (EAGAIN) count a dropped line without raising
def test_emit_fallback_send_eagain_counts_hwm_drop(mock_zmq):
    handler, socket = connected_handler(mock_zmq)
    socket.send.side_effect = zmq.ZMQError(zmq.EAGAIN)
    handler._last_hwm_warn = time.monotonic()
    record = make_record("raw message text")
    with patch.object(handler, "format", side_effect=ValueError("bad formatter")):
        handler.emit(record)
    assert handler._hwm_drops == 1
    assert handler._connected is True


# 4) close: release the socket with linger=0 and reset connection state
def test_close_releases_socket_and_resets_state(mock_zmq):
    handler, socket = connected_handler(mock_zmq)
    handler.close()
    socket.close.assert_called_once_with(linger=0)
    assert handler._socket is None
    assert handler._connected is False


# 4) close: safe no-op when there is no socket
def test_close_without_socket_is_safe(mock_zmq_no_address):
    handler = ZmqPushHandler()
    handler.close()
    assert handler._socket is None
    assert handler._connected is False


# 4) emit after close sends no data during the reconnect throttling interval
def test_emit_after_close_drops_line_within_reconnect_interval(mock_zmq, caplog):
    handler, socket = connected_handler(mock_zmq)
    handler.close()
    handler._last_reconnect_attempt = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        handler.emit(make_record("after close"))
    socket.send.assert_not_called()
    assert len(warnings_in(caplog, "collector unreachable")) == 1
