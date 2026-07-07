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
import threading
import uuid

import zmq

from motor.common.logger.logger_handler import CompressedRotatingFileHandler
from motor.config.log_config import LoggingConfig

logger = logging.getLogger("motor.log_collector")


class LogCollector:
    """Receives formatted log lines from all processes via ZMQ PULL and writes
    them to a single merged log file through a CompressedRotatingFileHandler.

    One instance per pod main process — child processes discover the collector
    address via the ``MOTOR_LOG_COLLECTOR_ADDRESS`` environment variable.
    """

    def __init__(self, log_file: str, config: LoggingConfig):
        self._log_file = log_file
        self._config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.PULL)
        self._socket.set_hwm(10000)

        # Bind to a unique IPC address so multiple pods on the same host don't
        # collide (unlikely but possible with hostPath volumes).
        self._address = f"ipc:///tmp/motor-log-{uuid.uuid4().hex[:8]}.sock"
        self._socket.bind(self._address)
        os.environ["MOTOR_LOG_COLLECTOR_ADDRESS"] = self._address

        self._file_handler = self._build_file_handler()

    @property
    def address(self) -> str:
        return self._address

    def _build_file_handler(self) -> CompressedRotatingFileHandler:
        log_dir = os.path.dirname(self._log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        handler = CompressedRotatingFileHandler(
            filename=self._log_file,
            maxBytes=self._config.log_rotation_size * 1024 * 1024,
            backupCount=self._config.log_rotation_count,
            compress=self._config.log_compress,
            compress_level=self._config.log_compress_level,
            max_total_size=self._config.log_max_total_size * 1024 * 1024,
            cleanup_interval=self._config.log_cleanup_interval,
        )
        # Use a passthrough formatter — log lines arrive pre-formatted from
        # ZmqPushHandler, so the file handler only needs to emit the raw message.
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def start(self) -> None:
        """Launch the collector loop in a daemon thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="log-collector",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "LogCollector started on %s, writing to %s",
            self._address,
            self._log_file,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the collector thread to exit and wait for it."""
        self._stop_event.set()
        # Push a sentinel so the blocking recv() unblocks.
        if self._thread is not None and self._thread.is_alive():
            try:
                sock = self._ctx.socket(zmq.PUSH)
                sock.connect(self._address)
                sock.send(b"__SHUTDOWN__", zmq.NOBLOCK)
                sock.close(linger=0)
            except zmq.ZMQError:
                logger.debug("LogCollector.stop: sentinel send failed, collector may already be down.")
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._file_handler.close()
        self._socket.close(linger=0)
        # Clean up the IPC socket file.
        ipc_path = self._address.replace("ipc://", "")
        try:
            if os.path.exists(ipc_path):
                os.remove(ipc_path)
        except OSError:
            pass

    def _run(self) -> None:
        """Main loop: receive formatted log lines and write to the shared file."""
        while not self._stop_event.is_set():
            try:
                if self._socket.poll(timeout=500):
                    msg = self._socket.recv(zmq.NOBLOCK)
                    if msg == b"__SHUTDOWN__":
                        continue
                    line = msg.decode("utf-8", errors="replace")
                    self._file_handler.emit(
                        logging.LogRecord(
                            name="merged",
                            level=logging.INFO,
                            pathname="",
                            lineno=0,
                            msg=line,
                            args=(),
                            exc_info=None,
                        )
                    )
            except zmq.ZMQError:
                continue
            except Exception:
                logger.exception("LogCollector: unexpected error in receive loop")
