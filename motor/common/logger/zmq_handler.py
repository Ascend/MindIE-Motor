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
import os
import time

import zmq


class ZmqPushHandler(logging.Handler):
    """Sends formatted log records to a :class:`LogCollector` via ZMQ PUSH.

    The collector address is discovered from the ``MOTOR_LOG_COLLECTOR_ADDRESS``
    environment variable. If the variable is absent or the socket cannot connect,
    ``emit()`` attempts to reconnect with a throttle interval; log lines are
    dropped only while the connection is unavailable so that the application is
    never blocked by log-collector issues.
    """

    # Minimum interval (seconds) between reconnection attempts.
    _RECONNECT_INTERVAL: float = 5.0

    # Minimum interval (seconds) between HWM-full warnings.
    _HWM_WARN_INTERVAL: float = 60.0

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self._connected = False
        self._warned = False
        self._socket: zmq.Socket | None = None
        self._address: str | None = None
        self._last_reconnect_attempt: float = 0.0
        self._hwm_drops: int = 0
        self._last_hwm_warn: float = 0.0

        address = os.environ.get("MOTOR_LOG_COLLECTOR_ADDRESS")
        if address:
            self._address = address
            self._connect()

    def _connect(self) -> None:
        try:
            ctx = zmq.Context.instance()
            self._socket = ctx.socket(zmq.PUSH)
            self._socket.set_hwm(10000)
            self._socket.connect(self._address)
            self._connected = True
        except zmq.ZMQError:
            self._connected = False

    def _try_reconnect(self) -> bool:
        """Attempt to reconnect to the collector, throttled by ``_RECONNECT_INTERVAL``.

        Returns ``True`` when the connection is (re)established, ``False``
        otherwise.  Resets the one-shot warning flag on success so that a
        future disconnection triggers a fresh warning.
        """
        if not self._address:
            return False
        now = time.monotonic()
        if now - self._last_reconnect_attempt < self._RECONNECT_INTERVAL:
            return False
        self._last_reconnect_attempt = now

        if self._socket is not None:
            try:
                self._socket.close(linger=0)
            except zmq.ZMQError:
                pass
            self._socket = None
        self._connected = False

        self._connect()
        if self._connected:
            self._warned = False
            logging.getLogger("motor.logger").info(
                "ZmqPushHandler: reconnected to collector at %s",
                self._address,
            )
        return self._connected

    def emit(self, record: logging.LogRecord) -> None:
        if not self._connected or self._socket is None:
            if not self._try_reconnect():
                if not self._warned:
                    logging.getLogger("motor.logger").warning(
                        "ZmqPushHandler: collector unreachable at %s, log line dropped "
                        "(this warning is emitted only once).",
                        self._address,
                    )
                    self._warned = True
                return

        try:
            formatted = self.format(record)
            self._socket.send(formatted.encode("utf-8"), zmq.NOBLOCK)
        except zmq.ZMQError as e:
            if e.errno == zmq.EAGAIN:
                # HWM full — buffer pressure, not a disconnection.
                # Drop the line; the collector will catch up.
                self._hwm_drops += 1
                now = time.monotonic()
                if now - self._last_hwm_warn >= self._HWM_WARN_INTERVAL:
                    logging.getLogger("motor.logger").warning(
                        "ZmqPushHandler: HWM full, %d log lines dropped since "
                        "last report (collector at %s may be overloaded).",
                        self._hwm_drops,
                        self._address,
                    )
                    self._hwm_drops = 0
                    self._last_hwm_warn = now
                return
            self._connected = False
            if not self._warned:
                logging.getLogger("motor.logger").warning(
                    "ZmqPushHandler: send to %s failed, log line dropped (this warning is emitted only once).",
                    self._address,
                )
                self._warned = True
            # Attempt immediate reconnect on send failure.
            self._try_reconnect()
        except Exception:
            # Record formatting error — fall back to raw message text.
            fallback = getattr(record, "message", "") or str(record.msg)
            try:
                self._socket.send(fallback.encode("utf-8"), zmq.NOBLOCK)
            except zmq.ZMQError as e:
                if e.errno == zmq.EAGAIN:
                    self._hwm_drops += 1
                    now = time.monotonic()
                    if now - self._last_hwm_warn >= self._HWM_WARN_INTERVAL:
                        logging.getLogger("motor.logger").warning(
                            "ZmqPushHandler: HWM full (fallback path), %d log "
                            "lines dropped since last report (collector at %s "
                            "may be overloaded).",
                            self._hwm_drops,
                            self._address,
                        )
                        self._hwm_drops = 0
                        self._last_hwm_warn = now
                    return
                self._connected = False
                self._try_reconnect()

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None
        self._connected = False
        super().close()
