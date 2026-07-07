# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import atexit
import logging
import multiprocessing
import os
import sys
from pathlib import Path

from motor.common.logger.formatter import ColoredFormatter, NewLineFormatter
from motor.common.logger.log_collector import LogCollector
from motor.common.logger.logger_handler import CompressedRotatingFileHandler
from motor.common.logger.zmq_handler import ZmqPushHandler
from motor.config.log_config import LoggingConfig


# Module-level LogCollector singleton — one per pod.
_collector: LogCollector | None = None

# Track the file handler currently attached to the vLLM root logger so that
# reconfiguration can replace it cleanly.
_vllm_attached_handler: logging.Handler | None = None


def _start_collector_if_needed(module_log_dir: str, config: LoggingConfig) -> None:
    """Start or restart the LogCollector for the current process.

    If a collector is already running it is stopped first so that
    ``reconfigure_logging`` picks up updated rotation/compression settings.
    """
    global _collector
    if os.environ.get("MOTOR_LOG_COLLECTOR_ADDRESS"):
        return  # Another process in this pod already started the collector.

    if _collector is not None:
        _collector.stop()
        _collector = None

    combined_log = os.path.join(module_log_dir, f"{hostname}.log")
    _collector = LogCollector(combined_log, config)
    _collector.start()


def _stop_collector() -> None:
    """Stop the LogCollector and clean up resources.  Registered via atexit."""
    global _collector
    if _collector is not None:
        _collector.stop()
        _collector = None


atexit.register(_stop_collector)


def attach_to_vllm_logger() -> None:
    """Attach the file handler from shared handlers to the vLLM root logger.

    Idempotent — safe to call multiple times (e.g. from
    ``_ensure_shared_handlers`` and later from vLLM engine startup).
    Handles ``reconfigure_logging`` correctly by tracking and replacing the
    previously-attached handler.
    """
    global _vllm_attached_handler
    if not _shared_handlers:
        return
    vllm_logger = logging.getLogger("vllm")

    # Remove a previously-attached handler so that reconfigure_logging does
    # not leave stale handlers on the vLLM logger.
    if _vllm_attached_handler is not None and _vllm_attached_handler in vllm_logger.handlers:
        vllm_logger.removeHandler(_vllm_attached_handler)
        _vllm_attached_handler = None

    # Attach the file handler (ZmqPushHandler or CompressedRotatingFileHandler).
    for handler in _shared_handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            continue
        if handler not in vllm_logger.handlers:
            vllm_logger.addHandler(handler)
            _vllm_attached_handler = handler
            break


# Set to track modules that have requested loggers
_logged_modules = set()

# Get hostname from en
hostname = os.getenv('HOSTNAME', 'unknown')
env_log_dir = os.getenv('MOTOR_LOG_PATH')

_MODULE_LOGGER_NAME = "common.logger"

# Top-level packages that use only the first level (e.g. "engine_server", "node_manager", "config").
_TOPLEVEL_COMPONENTS = frozenset({"engine_server", "node_manager", "config"})
# Top-level packages that use only the second level (e.g. "fault_tolerance", "domain", "http").
_SECONDLEVEL_COMPONENTS = frozenset({"controller", "coordinator", "common"})

# Third-party loggers that bypass the root WARNING safety net (own handler / own level).
# Always included in _get_third_party_logger_names() so they are covered even when the
# library has not yet instantiated the logger at suppression time.
_NOISY_THIRD_PARTY_LOGGERS = ("httpx", "httpcore", "urllib3", "uvicorn.error")

# Process-wide shared handler singletons. These are attached to every motor bucket logger
# (not to root) so that third-party libraries' INFO messages are not picked up.
_shared_handlers: list[logging.Handler] = []
# Names of motor bucket loggers that have already been wired with shared handlers.
_motor_buckets: set[str] = set()


class ProcessContextFilter(logging.Filter):
    """Inject process name into LogRecord for format placeholders."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.processName = multiprocessing.current_process().name
        return True


# Backward-compatible alias
ProcessNameFilter = ProcessContextFilter


class MaxLengthFormatter(logging.Formatter):
    """Wrap a formatter and cap total formatted output length."""

    def __init__(self, inner: logging.Formatter, max_length: int):
        super().__init__()
        self.inner = inner
        self.max_length = max_length

    def format(self, record: logging.LogRecord) -> str:
        msg = self.inner.format(record)
        if len(msg) > self.max_length:
            return msg[: self.max_length] + '...'
        return msg


class ApiAccessFilter(logging.Filter):
    """Suppress uvicorn access logs for specified APIs unless level >= configured level."""

    def __init__(self, api_filters: dict[str, int] = None):
        """
        Args:
            api_filters: dict mapping API paths to minimum log levels.
                        e.g., {"/heartbeat": logging.ERROR, "/register": logging.WARNING}
        """
        super().__init__()
        self.api_filters = api_filters or {}

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if record.name == "uvicorn.access":
            for path, min_level in self.api_filters.items():
                if path in message:
                    return record.levelno >= min_level
        return True


def _module_logger() -> logging.Logger:
    return logging.getLogger(_MODULE_LOGGER_NAME)


def _resolve_logger_name(name: str) -> str:
    if not name.startswith("motor."):
        return name
    parts = name.split('.')
    if len(parts) < 2:
        return name
    component = parts[1]
    if component in _TOPLEVEL_COMPONENTS:
        return component
    if component in _SECONDLEVEL_COMPONENTS and len(parts) >= 3:
        return parts[2]
    if len(parts) >= 3:
        return f"{parts[1]}.{parts[2]}"
    return component


def _use_color() -> bool:
    if os.environ.get('NO_COLOR'):
        return False
    color_flag = os.environ.get('MOTOR_LOGGING_COLOR')
    if color_flag == '0':
        return False
    if color_flag == '1':
        return True
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()


def _build_formatter(config: LoggingConfig, *, color: bool) -> MaxLengthFormatter:
    use_relpath = config.log_level.upper() == 'DEBUG'
    base_cls = ColoredFormatter if color else NewLineFormatter
    inner = base_cls(
        config.log_format,
        datefmt=config.log_date_format,
        use_relpath=use_relpath,
    )
    return MaxLengthFormatter(inner, config.log_max_line_length)


def _ensure_shared_handlers(config: LoggingConfig, log_dir: str | None) -> list[logging.Handler]:
    """Lazily build the process-wide shared handler set (console + optional file).

    Handlers are NOT attached to root; they are attached to each motor bucket logger
    by ``_attach_shared_handlers``. This keeps third-party logger INFO messages out
    of the motor output stream.
    """
    global _shared_handlers
    if _shared_handlers:
        return _shared_handlers

    level = getattr(logging, config.log_level.upper(), logging.INFO)
    process_filter = ProcessContextFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.addFilter(process_filter)
    console_handler.setFormatter(_build_formatter(config, color=_use_color()))

    handlers: list[logging.Handler] = [console_handler]

    if log_dir:
        # Bootstrap log: we may not yet have a motor bucket attached; write directly
        # to the console handler we just created to avoid losing the message.
        console_handler.emit(
            logging.LogRecord(
                name=_MODULE_LOGGER_NAME,
                level=logging.INFO,
                pathname=__file__,
                lineno=0,
                msg="Internal logs of pod will be saved to %s, will mounted to host %s",
                args=(log_dir, config.host_log_dir),
                exc_info=None,
            )
        )

        # Get log_dir from pod name prefix, remove random suffix
        parts = hostname.split('-')
        if len(parts) <= 2:
            module_log_dir = os.path.join(log_dir, hostname)
        else:
            module_log_dir = os.path.join(log_dir, '-'.join(parts[:-2]))

        if not os.path.exists(module_log_dir):
            try:
                Path(module_log_dir).mkdir(parents=True, exist_ok=True)
            except Exception:
                console_handler.emit(
                    logging.LogRecord(
                        name=_MODULE_LOGGER_NAME,
                        level=logging.ERROR,
                        pathname=__file__,
                        lineno=0,
                        msg="Failed to create log directory: %s",
                        args=(module_log_dir,),
                        exc_info=None,
                    )
                )
        if os.path.exists(module_log_dir):
            try:
                if config.log_collector_enabled:
                    _start_collector_if_needed(module_log_dir, config)
                    file_handler = ZmqPushHandler()
                else:
                    _stop_collector()
                    log_file = os.path.join(module_log_dir, f"{hostname}_{os.getpid()}.log")
                    file_handler = CompressedRotatingFileHandler(
                        filename=log_file,
                        maxBytes=config.log_rotation_size * 1024 * 1024,
                        backupCount=config.log_rotation_count,
                        compress=config.log_compress,
                        compress_level=config.log_compress_level,
                        max_total_size=config.log_max_total_size * 1024 * 1024,
                        cleanup_interval=config.log_cleanup_interval,
                    )
                file_handler.addFilter(process_filter)
                file_handler.setFormatter(_build_formatter(config, color=False))
                handlers.append(file_handler)
            except Exception:
                console_handler.emit(
                    logging.LogRecord(
                        name=_MODULE_LOGGER_NAME,
                        level=logging.ERROR,
                        pathname=__file__,
                        lineno=0,
                        msg="Failed to configure log handler",
                        args=(),
                        exc_info=None,
                    )
                )

    _shared_handlers = handlers

    # If vLLM has already been imported, route its logs to the shared file
    # handler as well.
    attach_to_vllm_logger()

    return handlers


def _attach_shared_handlers(bucket: logging.Logger) -> None:
    """Attach the shared handler set to a motor bucket logger.

    Handlers live on the bucket logger, not on root.  Propagation is disabled
    in production to prevent double emission when root accidentally picks up a
    handler (e.g. from ``logging.basicConfig()`` in a third-party lib, an
    example script, or a side effect during import).

    In pytest the propagation guard is skipped so that ``caplog`` (which hooks
    into root) can still capture motor bucket log records.

    Third-party loggers (httpx, urllib3, …) use their own names and never flow
    through a motor bucket, so disabling motor-bucket propagation does NOT
    silence them — they still propagate up to root on their own.
    """
    if bucket.name in _motor_buckets:
        return
    # Keep propagation in pytest so caplog works.  Use sys.modules instead of
    # PYTEST_CURRENT_TEST because module-level get_logger(__name__) calls
    # happen during test collection, before the env var is set.
    if 'pytest' not in sys.modules:
        bucket.propagate = False
    for handler in _shared_handlers:
        bucket.addHandler(handler)
    _motor_buckets.add(bucket.name)


def get_logger(name: str = __name__, level: int | None = None):
    """
    Get or create a logger with enhanced capabilities.

    Args:
        name: Logger name (usually __name__)
        level: Optional logging level (overrides config)

    Returns:
        Configured logger instance
    """
    # Record this module as having requested a logger
    _logged_modules.add(name)

    # Get configuration for this specific module (use default config initially)
    config = LoggingConfig()

    # Use provided parameters or fall back to config
    if level is None:
        level = getattr(logging, config.log_level.upper(), logging.INFO)

    log_name = _resolve_logger_name(name)
    logger = logging.getLogger(log_name)

    # Lazily build shared handlers (console + optional file) before attaching.
    _ensure_shared_handlers(config, env_log_dir)
    _attach_shared_handlers(logger)

    # If level is overridden by the caller, honor it; otherwise keep logger at NOTSET
    # so that records pass through and the per-bucket level (set by reconfigure_logging
    # or by the implicit default below) decides what gets emitted.
    if level is not None:
        logger.setLevel(level)

    return logger


def _get_third_party_logger_names() -> set[str]:
    """Return all currently registered non-motor logger names.

    Walks ``logging.root.manager.loggerDict`` and filters out motor-internal
    loggers.  Also ensures the known noisy set is included so that loggers
    not yet instantiated are covered when a universal default is requested.
    """
    names: set[str] = set()
    manager = getattr(logging.root, 'manager', None)
    logger_dict: dict = getattr(manager, 'loggerDict', {}) if manager is not None else {}
    for logger_name in list(logger_dict):
        if not logger_name.startswith("motor.") and logger_name not in _motor_buckets:
            names.add(logger_name)
    names.update(_NOISY_THIRD_PARTY_LOGGERS)
    return names


def _resolve_log_level(level_str: str) -> int:
    """Convert a level string to a ``logging`` level constant.

    Logs a warning and falls back to WARNING on unrecognized strings.
    """
    level = getattr(logging, level_str.upper(), None)
    if level is None:
        _module_logger().warning(
            "Unknown log level '%s', falling back to WARNING",
            level_str,
        )
        return logging.WARNING
    return level


def _suppress_noisy_third_party_loggers(config: LoggingConfig) -> None:
    """Apply configured log levels to third-party loggers.

    Third-party loggers are always set to the configured levels,
    defaulting to WARNING regardless of motor's own log_level.

    Resolution order (finer overrides coarser):

    1. ``third_party_log_levels`` is **None** or absent → fall back to
       ``{"default": "WARNING"}`` (all third-party loggers to WARNING).
    2. ``third_party_log_levels`` is a dict → the ``"default"`` key (if
       present) is the fallback level; specific logger-name keys override
       ``"default"``.
    """
    third_party = config.third_party_log_levels

    if third_party is None:
        third_party = {"default": "WARNING"}

    # Resolve fallback level when "default" key is missing.
    default_level_str = third_party.get("default", "WARNING")

    for name in _get_third_party_logger_names():
        if name in third_party:
            level_str = third_party[name]
        else:
            level_str = default_level_str

        if not isinstance(level_str, str):
            _module_logger().warning(
                "Invalid third-party log level for '%s': %r (expected a string), skipping",
                name,
                level_str,
            )
            continue

        resolved = _resolve_log_level(level_str)
        logging.getLogger(name).setLevel(resolved)


def reconfigure_logging(log_config: LoggingConfig) -> None:
    """
    Reconfigure logging using a new LoggingConfig.

    Behaviour follows the vLLM pattern:

    - Shared motor handlers are (re)created and attached to every previously seen
      motor bucket logger. Root logger is left untouched at Python's WARNING default.
    - The level of every motor bucket logger is updated to the new level.
    - Third-party loggers are always set to configured levels (default WARNING)
      regardless of motor's own log_level.
    """
    # Check if we're running in pytest (to avoid breaking caplog)
    is_pytest = os.environ.get('PYTEST_CURRENT_TEST') is not None
    if is_pytest:
        return

    global _shared_handlers
    new_level = getattr(logging, log_config.log_level.upper(), logging.INFO)

    # Rebuild shared handlers from the new config. Existing handlers (from a
    # previous get_logger call with default config) are detached first so we do
    # not double-emit when the formatter/level changes.
    for handler in _shared_handlers:
        for bucket_name in list(_motor_buckets):
            logging.getLogger(bucket_name).removeHandler(handler)
    _shared_handlers = []
    new_handlers = _ensure_shared_handlers(log_config, env_log_dir)

    for bucket_name in list(_motor_buckets):
        bucket = logging.getLogger(bucket_name)
        for handler in new_handlers:
            bucket.addHandler(handler)
        bucket.setLevel(new_level)

    _suppress_noisy_third_party_loggers(log_config)

    _module_logger().info("Logging reconfigured with level: %s", log_config.log_level)
