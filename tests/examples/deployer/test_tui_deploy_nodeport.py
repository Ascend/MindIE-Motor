# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""The TUI deploy path must answer NodePort conflicts through its own line editor."""

from unittest.mock import patch

import pytest

import lib.constant as C
from lib import nodeport_allocator
from lib.tui.actions import DeployAborted
from lib.tui.session import DeployInteractiveSession


class _FakeTerminal:
    """TerminalContext stand-in that records writes instead of touching the tty."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def flush_input(self) -> None:
        pass

    def _write(self, text: str) -> None:
        self.writes.append(text)

    def write_at(self, row: int, col: int, text: str) -> None:
        self.writes.append(text)

    def move_to(self, row: int, col: int = 1) -> None:
        pass

    def clear(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clear_conflict_prompt_reader():
    yield
    nodeport_allocator.set_conflict_prompt_reader(None)


class _KeyTerminal:
    """Terminal stand-in that replays a fixed keystroke sequence."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)

    def poll_key(self, timeout: float = 0.0) -> str | None:
        return self._keys.pop(0) if self._keys else None

    def write_at(self, row: int, col: int, text: str) -> None:
        pass

    def move_to(self, row: int, col: int = 1) -> None:
        pass


def _make_session() -> DeployInteractiveSession:
    session = DeployInteractiveSession("", 0, {}, deployed=False)
    session._ctx = _FakeTerminal()
    return session


@pytest.fixture
def deploy_env(tmp_path, monkeypatch):
    """Minimal config dir plus deploy-side mocks so ``_deploy_service`` runs headless."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "user_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(tmp_path / "output_yamls"))
    with (
        patch("lib.config_validator.resolve_config_paths", return_value=(str(cfg_dir / "user_config.json"), None)),
        patch("lib.utils.read_json", return_value={C.MOTOR_DEPLOY_CONFIG: {C.CONFIG_JOB_ID: "ns-test"}}),
        patch("lib.generator.k8s_utils.set_user_config_path"),
        patch("lib.container_snapshot.validate_container_snapshot_config"),
        patch("lib.generator.engine.validate_instance_nums"),
    ):
        yield tmp_path


def test_deploy_installs_tui_nodeport_reader(deploy_env):
    """R inside the TUI must hand NodePort prompts to the raw-mode editor."""
    session = _make_session()
    session.deployer_dir = str(deploy_env)
    session._read_line = lambda prompt, default="", **_kwargs: "cfg"

    seen = {}

    def _fake_deploy(user_config, env_config_path, dry_run=False, auto_log_collect=False):
        seen["reader"] = nodeport_allocator._conflict_prompt_reader

    with patch("deploy.deploy_services", side_effect=_fake_deploy):
        session._deploy_service()

    assert seen["reader"] == session._prompt_nodeport_conflict
    assert nodeport_allocator._conflict_prompt_reader is None
    assert session._deployed is True
    assert session.namespace == "ns-test"


def test_deploy_aborted_by_ctrl_c_returns_to_menu(deploy_env):
    """Ctrl-C at the conflict prompt stops the deploy but keeps the TUI alive."""
    session = _make_session()
    session.deployer_dir = str(deploy_env)
    session._read_line = lambda prompt, default="", **_kwargs: "cfg"

    def _aborting_deploy(user_config, env_config_path, dry_run=False, auto_log_collect=False):
        raise DeployAborted("deploy aborted by user")

    with patch("deploy.deploy_services", side_effect=_aborting_deploy):
        session._deploy_service()

    assert session._deployed is False
    assert session._status_msg is not None
    assert "aborted" in session._status_msg
    assert nodeport_allocator._conflict_prompt_reader is None


def test_nodeport_prompt_reads_through_line_editor():
    """The conflict answer comes from _read_line and the status line is restored."""
    session = _make_session()
    calls = []

    def _fake_read_line(prompt, default="", *, ctrl_c_aborts=False):
        calls.append((prompt, ctrl_c_aborts))
        return "31520"

    session._read_line = _fake_read_line

    assert session._prompt_nodeport_conflict("[NodePort] 31015 (Coordinator infer) -> y / <port> / N: ") == "31520"
    assert calls == [("[NodePort] 31015 (Coordinator infer) -> y / <port> / N: ", True)]
    assert "Deploying" in session._status_msg


def test_nodeport_prompt_drops_keys_typed_during_deploy():
    """Stale keystrokes from the deploy wait must not answer the prompt unseen."""
    session = _make_session()
    order = []

    def _flush() -> None:
        order.append("flush")

    def _fake_read_line(prompt, default="", *, ctrl_c_aborts=False) -> None:
        order.append("read")

    session._ctx.flush_input = _flush
    session._read_line = _fake_read_line

    assert session._prompt_nodeport_conflict("prompt") is None
    assert order == ["flush", "read"]


def test_nodeport_prompt_without_terminal_cancels():
    """Without a terminal the prompt cannot be answered: cancel (keep the port)."""
    session = _make_session()
    session._ctx = None

    assert session._prompt_nodeport_conflict("prompt") is None


def test_read_line_ctrl_c_aborts_deploy():
    """The conflict prompt maps Ctrl-C to an abort, like the CLI."""
    session = _make_session()
    session._ctx = _KeyTerminal(["\x03"])
    session._running = True

    with pytest.raises(DeployAborted):
        session._read_line("prompt", ctrl_c_aborts=True)


def test_read_line_ctrl_c_still_cancels_locally_by_default():
    """Other TUI inputs keep treating Ctrl-C as a local cancel."""
    session = _make_session()
    session._ctx = _KeyTerminal(["\x03"])
    session._running = True

    assert session._read_line("prompt") is None
