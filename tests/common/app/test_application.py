# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Daemon lifecycle tests use bounded input and never register real signal handlers."""

import io
import signal
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from motor.common.app import application


class SampleApplication(application.Application):
    """Minimal concrete application exercising the base lifecycle."""

    def init_modules(self) -> None:
        self.add_module("worker", Mock(spec=["stop", "update_config"]))


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> SampleApplication:
    monkeypatch.setattr(application, "log_configuration_summary", Mock())
    return SampleApplication(SimpleNamespace(config_path=None), check_interval=0.01)


def test_modules_stop_in_reverse_order_even_after_failure(app: SampleApplication) -> None:
    stopped = []

    async def async_stop() -> None:
        stopped.append("async")

    def failed_stop() -> None:
        stopped.append("failed")
        raise RuntimeError("shutdown failed")

    app.add_module("first", SimpleNamespace(stop=lambda: stopped.append("first")))
    app.add_module("failed", SimpleNamespace(stop=failed_stop))
    app.add_module("async", SimpleNamespace(stop=async_stop))
    app.add_module("no-stop", object())
    assert app.get_module("async") is app.modules["async"]
    assert app.get_module("missing") is None
    app.stop_all_modules()
    assert stopped == ["async", "failed", "first"]
    assert app.modules == {}


def test_config_updates_reach_remaining_modules_after_failure(app: SampleApplication) -> None:
    failing = Mock(spec=["update_config"])
    failing.update_config.side_effect = ValueError("bad config")
    healthy = Mock(spec=["update_config"])
    app.add_module("failing", failing)
    app.add_module("healthy", healthy)
    app.add_module("no-update", object())
    app.on_config_updated()
    failing.update_config.assert_called_once_with(app.config)
    healthy.update_config.assert_called_once_with(app.config)


@pytest.mark.parametrize("has_path", [False, True])
def test_watcher_starts_once_and_is_stopped_on_shutdown(
    app: SampleApplication, monkeypatch: pytest.MonkeyPatch, has_path: bool
) -> None:
    watcher = Mock(spec=["stop"]) if has_path else None
    start = Mock(return_value=watcher)
    monkeypatch.setattr(application, "start_config_file_watcher", start)
    app._start_config_watcher()
    if has_path:
        app._start_config_watcher()
    start.assert_called_once_with(app.config, app.on_config_updated)
    app.shutdown()
    app.shutdown()
    if watcher is not None:
        watcher.stop.assert_called_once()
    assert app._config_watcher is None


@pytest.mark.parametrize("failure", [None, ValueError("not main thread"), OSError("unsupported")])
def test_signal_registration_attempts_both_signals_and_handler_stops_app(
    app: SampleApplication, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    register = Mock(side_effect=failure)
    monkeypatch.setattr(application.signal, "signal", register)
    app.setup_signal_handlers()
    assert register.call_args_list == [
        call(signal.SIGINT, app._handle_signal),
        call(signal.SIGTERM, app._handle_signal),
    ]
    app._handle_signal(signal.SIGTERM, None)
    assert app.stop_event.is_set()


@pytest.mark.parametrize("text", ["stop\n", "  STOP  \n", "ignored\nstop\n"])
def test_daemon_accepts_stop_command_and_ignores_other_input(
    app: SampleApplication, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    stdin = io.StringIO(text)
    monkeypatch.setattr(application.sys, "stdin", stdin)
    select_input = Mock(side_effect=[([stdin], [], []) for _ in text.splitlines()])
    monkeypatch.setattr(application.select, "select", select_input)
    tick = Mock()
    app._daemon_loop(on_tick=tick)
    assert app.stop_event.is_set()
    assert tick.call_count == len(text.splitlines())
    select_input.assert_called_with([stdin], [], [], 0.01)


@pytest.mark.parametrize("select_error", [None, OSError("closed stdin")])
def test_daemon_waits_instead_of_spinning_on_eof_or_closed_stdin(
    app: SampleApplication, monkeypatch: pytest.MonkeyPatch, select_error: Exception | None
) -> None:
    stdin = io.StringIO("")
    monkeypatch.setattr(application.sys, "stdin", stdin)
    monkeypatch.setattr(application.select, "select", Mock(return_value=([stdin], [], []), side_effect=select_error))
    wait = Mock(side_effect=lambda _timeout: app.stop_event.set())
    monkeypatch.setattr(app.stop_event, "wait", wait)
    app._daemon_loop()
    wait.assert_called_once_with(0.01)
    assert app.stop_event.is_set()


def test_daemon_handles_timeout_then_keyboard_interrupt(
    app: SampleApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    select_input = Mock(side_effect=[([], [], []), KeyboardInterrupt()])
    monkeypatch.setattr(application.select, "select", select_input)
    app._daemon_loop()
    assert select_input.call_count == 2
    assert app.stop_event.is_set()


@pytest.mark.parametrize("banner", ["", "test daemon"])
def test_run_initializes_then_cleans_modules_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch, banner: str
) -> None:
    monkeypatch.setattr(application, "log_configuration_summary", Mock())
    app = SampleApplication(None, banner_label=banner)
    banner_logger = Mock()
    monkeypatch.setattr(application, "log_startup_banner", banner_logger)
    monkeypatch.setattr(application.signal, "signal", Mock())
    stdin = io.StringIO("stop\n")
    monkeypatch.setattr(application.sys, "stdin", stdin)
    monkeypatch.setattr(application.select, "select", Mock(side_effect=[([stdin], [], [])]))
    modules = []
    original_init = app.init_modules

    def initialize() -> None:
        original_init()
        modules.append(app.get_module("worker"))

    monkeypatch.setattr(app, "init_modules", initialize)
    assert app.run() == -1
    modules[0].stop.assert_called_once()
    assert app.modules == {}
    assert app.stop_event.is_set()
    assert banner_logger.call_count == bool(banner)
