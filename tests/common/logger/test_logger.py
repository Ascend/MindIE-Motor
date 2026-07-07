# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import logging
import sys

import pytest

from motor.common.logger import logger as logger_module
from motor.common.logger.formatter import ColoredFormatter, NewLineFormatter
from motor.common.logger.logger import (
    MaxLengthFormatter,
    _resolve_logger_name,
    _suppress_noisy_third_party_loggers,
    get_logger,
    reconfigure_logging,
)
from motor.config.log_config import LoggingConfig


class TestResolveLoggerName:
    def test_toplevel_components_use_first_level(self):
        assert _resolve_logger_name("motor.engine_server.core.vllm_engine") == "engine_server"
        assert _resolve_logger_name("motor.engine_server.cli.main") == "engine_server"
        assert _resolve_logger_name("motor.node_manager.api_client.controller_api_client") == "node_manager"
        assert _resolve_logger_name("motor.config.controller") == "config"
        assert _resolve_logger_name("motor.config.coordinator") == "config"

    def test_secondlevel_components_use_second_level(self):
        assert _resolve_logger_name("motor.controller.fault_tolerance.k8s.resource_monitor") == "fault_tolerance"
        assert _resolve_logger_name("motor.coordinator.api_server.management_server") == "api_server"
        assert _resolve_logger_name("motor.common.etcd.etcd_client") == "etcd"

    def test_non_motor_name_unchanged(self):
        assert _resolve_logger_name("uvicorn.error") == "uvicorn.error"


class TestLogFormatter:
    @pytest.fixture
    def record(self):
        record = logging.LogRecord(
            name="engine_server",
            level=logging.INFO,
            pathname="/app/motor/engine_server/cli/main.py",
            lineno=31,
            msg="successfully parsed vllm engine configuration",
            args=(),
            exc_info=None,
        )
        record.filename = "main.py"
        record.processName = "MainProcess"
        record.process = 412
        return record

    def test_newline_formatter_output(self, record):
        config = LoggingConfig()
        formatter = NewLineFormatter(config.log_format, datefmt=config.log_date_format)
        output = formatter.format(record)
        assert output.startswith("(MainProcess pid=412) INFO ")
        assert "[engine_server][main.py:31]" in output
        assert output.endswith("successfully parsed vllm engine configuration")

    def test_colored_formatter_adds_ansi(self, record):
        config = LoggingConfig()
        formatter = ColoredFormatter(config.log_format, datefmt=config.log_date_format)
        output = formatter.format(record)
        assert "\033[32mINFO\033[0m" in output
        assert "\033[90m" in output

    def test_default_date_format(self):
        assert LoggingConfig().log_date_format == "%m-%d %H:%M:%S"

    def test_default_log_format_has_process_and_module(self):
        fmt = LoggingConfig().log_format
        assert "%(processName)s pid=%(process)d)" in fmt
        assert "[%(name)s][%(fileinfo)s:%(lineno)d]" in fmt

    def test_max_length_formatter_keeps_multiline_traceback(self):
        config = LoggingConfig()

        def _capture_exc_info():
            try:
                raise ValueError("boom")
            except ValueError:
                return sys.exc_info()
            raise AssertionError("expected ValueError")

        exc_info = _capture_exc_info()

        record = logging.LogRecord(
            name="engine_server",
            level=logging.ERROR,
            pathname="/app/motor/engine_server/cli/dispatch.py",
            lineno=223,
            msg="Error occurred",
            args=(),
            exc_info=exc_info,
        )
        record.filename = "dispatch.py"
        record.processName = "MainProcess"
        record.process = 47

        formatter = MaxLengthFormatter(
            NewLineFormatter(config.log_format, datefmt=config.log_date_format),
            config.log_max_line_length,
        )
        output = formatter.format(record)

        assert "\\r\\n" not in output
        assert "Traceback (most recent call last):" in output
        assert output.count("\n") >= 2
        assert output.count("(MainProcess pid=47) ERROR ") >= 2

    def test_max_length_formatter_truncates_long_output(self, record):
        config = LoggingConfig()
        formatter = MaxLengthFormatter(
            NewLineFormatter(config.log_format, datefmt=config.log_date_format),
            max_length=80,
        )
        output = formatter.format(record)
        assert len(output) == 83
        assert output.endswith("...")


class TestThirdPartySuppression:
    """Verify the vLLM-style third-party logger suppression is wired correctly.

    These tests manipulate the module-level ``_shared_handlers`` / ``_motor_buckets``
    singletons in ``motor.common.logger.logger``. A ``reset_singletons`` fixture
    restores them so other test classes (and re-runs) are not affected.
    """

    @pytest.fixture
    def reset_singletons(self):
        def snapshot_loggers():
            loggers = {"": logging.getLogger()}
            for name, logger in logging.Logger.manager.loggerDict.items():
                if isinstance(logger, logging.Logger):
                    loggers[name] = logger
            return {
                name: {
                    "level": logger.level,
                    "propagate": logger.propagate,
                    "disabled": logger.disabled,
                    "handlers": list(logger.handlers),
                    "filters": list(logger.filters),
                }
                for name, logger in loggers.items()
            }

        def restore_logger_state(snapshot):
            current_names = {""}
            current_names.update(
                name for name, logger in logging.Logger.manager.loggerDict.items() if isinstance(logger, logging.Logger)
            )
            for name in current_names:
                logger = logging.getLogger(name)
                state = snapshot.get(name)
                if state is None:
                    logger.handlers = []
                    logger.filters = []
                    logger.setLevel(logging.NOTSET)
                    logger.propagate = True
                    logger.disabled = False
                    continue
                logger.handlers = list(state["handlers"])
                logger.filters = list(state["filters"])
                logger.setLevel(state["level"])
                logger.propagate = state["propagate"]
                logger.disabled = state["disabled"]

        logger_snapshot = snapshot_loggers()
        original_handlers = list(logger_module._shared_handlers)
        original_buckets = set(logger_module._motor_buckets)
        original_logged_modules = set(logger_module._logged_modules)
        original_root_handlers = list(logging.getLogger().handlers)
        logger_module._shared_handlers = []
        logger_module._motor_buckets = set()
        # Detach any handlers that earlier cases (or the real bootstrap) put on root.
        for h in original_root_handlers:
            logging.getLogger().removeHandler(h)
        try:
            yield
        finally:
            for h in logger_module._shared_handlers:
                for bucket_name in list(logger_module._motor_buckets):
                    logging.getLogger(bucket_name).removeHandler(h)
            logger_module._shared_handlers = original_handlers
            logger_module._motor_buckets = original_buckets
            logger_module._logged_modules = original_logged_modules
            restore_logger_state(logger_snapshot)
            for h in original_root_handlers:
                if h not in logging.getLogger().handlers:
                    logging.getLogger().addHandler(h)

    def test_get_logger_does_not_attach_to_root(self, reset_singletons, capsys):
        """get_logger must wire motor buckets; root must not carry motor handlers."""
        logger = get_logger("motor.coordinator.api_server.management_server")

        assert logger.handlers  # at least one shared handler attached
        # None of the motor shared handlers may be installed on root — that is
        # the whole point of the vLLM-style design. (pytest's LogCaptureHandler
        # may also be on root; we only care that motor handlers are not.)
        root_handlers = set(logging.getLogger().handlers)
        motor_handlers = set(logger_module._shared_handlers)
        assert motor_handlers.isdisjoint(root_handlers)
        assert "api_server" in logger_module._motor_buckets

        logger.info("hello-from-motor")
        captured = capsys.readouterr().err + capsys.readouterr().out
        # (consume re-fill because readouterr resets; re-read for assertion)
        logger.info("hello-from-motor-2")
        captured = capsys.readouterr().out
        assert "hello-from-motor-2" in captured

    def test_third_party_info_filtered_by_root_warning(self, reset_singletons, capsys):
        """httpx INFO must NOT appear in motor output (root WARNING safety net)."""
        get_logger("motor.coordinator.api_server.management_server")

        logging.getLogger("httpx").info("httpx-verbose-should-not-appear")
        captured = capsys.readouterr().out
        assert "httpx-verbose-should-not-appear" not in captured

    def test_third_party_warning_not_routed_to_motor_buckets(self, reset_singletons):
        """httpx WARNING must not flow into motor bucket handle().  Motor bucket
        propagation is disabled in production, and httpx records live on a
        separate logger tree — propagation is one-directional (parent, not child
        → parent in the reverse direction), so httpx noise never reaches a motor
        bucket's handle().
        """
        bucket = get_logger("motor.coordinator.api_server.management_server")

        # The httpx logger has no parent relationship with our motor bucket,
        # so a WARNING emit on httpx must not flow into our motor stream.
        # We assert by checking that the call is a no-op for the bucket's
        # handlers (no LogRecord passed through).
        propagated_into_bucket = False
        original_handle = bucket.handle

        def spy_handle(record):
            nonlocal propagated_into_bucket
            if record.name == "httpx":
                propagated_into_bucket = True
            return original_handle(record)

        bucket.handle = spy_handle
        try:
            logging.getLogger("httpx").warning("httpx-warning-noise")
        finally:
            bucket.handle = original_handle

        assert propagated_into_bucket is False

    def test_suppress_noisy_libs_at_all_levels(self, reset_singletons):
        """Point-kill fires regardless of motor log_level (default WARNING)."""
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.NOTSET)

        _suppress_noisy_third_party_loggers(LoggingConfig(log_level="INFO"))
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            assert logging.getLogger(name).level == logging.WARNING

        # DEBUG should also suppress to WARNING (default via None → {"default": "WARNING"}).
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.NOTSET)
        _suppress_noisy_third_party_loggers(LoggingConfig(log_level="DEBUG"))
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            assert logging.getLogger(name).level == logging.WARNING

    def test_reconfigure_updates_buckets_and_resuppresses(self, reset_singletons, monkeypatch):
        """reconfigure_logging must move buckets to the new level and re-apply suppression."""
        # Simulate first-touch: a motor bucket logger exists, motor is at INFO.
        bucket_logger = get_logger("motor.coordinator.api_server.management_server")
        # Pre-seed: point httpx at NOTSET so reconfigure must bump it to WARNING.
        logging.getLogger("httpx").setLevel(logging.NOTSET)

        # pytest env normally skips reconfigure_logging; unset the env var so
        # the function actually runs.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        reconfigure_logging(LoggingConfig(log_level="INFO"))

        assert bucket_logger.level == logging.INFO
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_reconfigure_does_not_touch_root_handlers(self, reset_singletons, monkeypatch):
        """Even after reconfigure, root must remain free of motor handlers."""
        get_logger("motor.coordinator.api_server.management_server")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        reconfigure_logging(LoggingConfig(log_level="DEBUG"))

        root_handlers = set(logging.getLogger().handlers)
        motor_handlers = set(logger_module._shared_handlers)
        assert motor_handlers.isdisjoint(root_handlers)
        # Buckets follow the new level.
        assert logging.getLogger("api_server").level == logging.DEBUG

    def test_default_key_applies_to_all_third_party(self, reset_singletons):
        """``{"default": "ERROR"}`` forces all known third-party loggers to ERROR."""
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.NOTSET)

        _suppress_noisy_third_party_loggers(
            LoggingConfig(log_level="INFO", third_party_log_levels={"default": "ERROR"})
        )
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            assert logging.getLogger(name).level == logging.ERROR

    def test_default_with_specific_override(self, reset_singletons):
        """Specific logger key overrides ``"default"``."""
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.NOTSET)

        _suppress_noisy_third_party_loggers(
            LoggingConfig(
                log_level="INFO",
                third_party_log_levels={
                    "default": "WARNING",
                    "httpx": "ERROR",
                    "uvicorn.error": "DEBUG",
                },
            )
        )
        assert logging.getLogger("httpx").level == logging.ERROR
        assert logging.getLogger("httpcore").level == logging.WARNING  # from default
        assert logging.getLogger("urllib3").level == logging.WARNING  # from default
        assert logging.getLogger("uvicorn.error").level == logging.DEBUG

    def test_default_behavior_unchanged_when_none(self, reset_singletons):
        """When ``third_party_log_levels`` is None, all discovered third-party loggers → WARNING."""
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.NOTSET)

        _suppress_noisy_third_party_loggers(LoggingConfig(log_level="INFO", third_party_log_levels=None))
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            assert logging.getLogger(name).level == logging.WARNING

    def test_third_party_log_levels_applied_when_motor_debug(self, reset_singletons):
        """When motor log_level is DEBUG, third_party_log_levels still takes effect."""
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.NOTSET)

        _suppress_noisy_third_party_loggers(
            LoggingConfig(
                log_level="DEBUG",
                third_party_log_levels={"default": "ERROR"},
            )
        )
        for name in ("httpx", "httpcore", "urllib3", "uvicorn.error"):
            assert logging.getLogger(name).level == logging.ERROR
