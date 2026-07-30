# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import sys
from unittest.mock import patch, MagicMock

from motor.config.node_manager import KVCacheStoreConfig
from motor.node_manager.core.services.memcache.lifecycle import LocalService


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_ls(**overrides) -> LocalService:
    """Quick one-off LocalService with overrides on top of defaults."""
    cfg = KVCacheStoreConfig()
    cfg.enable = True
    cfg.backend = "memcache"
    cfg.service = "kvs-master:50088"
    cfg.port = 50088
    cfg.local_service_mode = "inprocess"
    cfg.local_config_path = "/tmp/mmc-local-inprocess.conf"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return LocalService(
        hardware_type="800I_A3",
        kv_cache_store_config=cfg,
        local_world_size=4,
    )


def _write_conf(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _cleanup(*paths: str) -> None:
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


# ===================================================================
# should_launch
# ===================================================================


def test_should_launch_standalone():
    assert _make_ls(local_service_mode="standalone").should_launch() is True


def test_should_launch_inprocess():
    assert _make_ls(local_service_mode="inprocess").should_launch() is False


def test_should_launch_disabled():
    assert _make_ls(enable=False, local_service_mode="standalone").should_launch() is False


def test_should_launch_not_memcache():
    assert _make_ls(backend="mooncake", local_service_mode="standalone").should_launch() is False


# ===================================================================
# prepare()
# ===================================================================


def test_prepare_sets_mmc_local_config_path_env():
    """prepare() exports MMC_LOCAL_CONFIG_PATH env var pointing to the conf file."""
    ls = _make_ls(local_service_mode="inprocess")
    ls._endpoints_count = 1
    _write_conf("/tmp/mmc-local-inprocess.conf", "")
    try:
        ls.prepare(endpoints_count=1)
        assert os.environ["MMC_LOCAL_CONFIG_PATH"] == "/tmp/mmc-local-inprocess.conf"
    finally:
        _cleanup("/tmp/mmc-local-inprocess.conf")


def test_prepare_no_conf_warns(caplog):
    """When conf file doesn't exist, prepare() warns and returns early."""
    ls = _make_ls(local_service_mode="inprocess")
    ls._kv_cfg.local_config_path = "/tmp/nonexistent.conf"
    ls.prepare(endpoints_count=0)
    assert "not found" in caplog.text


def test_prepare_a2_a5_force_inprocess():
    """A2 hardware forces inprocess mode even if standalone is configured."""
    ls = _make_ls(local_service_mode="standalone")
    ls.hardware_type = "800I_A2"
    _write_conf("/tmp/mmc-local-inprocess.conf", "")
    try:
        ls.prepare(endpoints_count=0)
        assert ls._kv_cfg.local_service_mode == "inprocess"
        assert ls.should_launch() is False
    finally:
        _cleanup("/tmp/mmc-local-inprocess.conf")


# ===================================================================
# pull() — standalone LS (Popen)
# ===================================================================


@patch("subprocess.Popen")
def test_pull_launches_standalone_as_subprocess(mock_popen):
    """pull() starts standalone LS via Popen with standalone conf in env."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # process still running
    mock_popen.return_value = mock_proc

    _write_conf("/tmp/mmc-local-standalone.conf", "")
    try:
        ls = _make_ls(local_service_mode="standalone")
        ls._kv_cfg.local_config_path = "/tmp/mmc-local-inprocess.conf"
        ls.pull()

        # Verify Popen was called with sys.executable -m
        mock_popen.assert_called_once()
        call_args, call_kwargs = mock_popen.call_args
        cmd = call_args[0]
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"
        assert cmd[2] == "motor.node_manager.core.services.memcache.worker"
        assert call_kwargs["shell"] is False
        # Env should have standalone conf path
        assert call_kwargs["env"]["MMC_LOCAL_CONFIG_PATH"] == "/tmp/mmc-local-standalone.conf"

        assert ls.is_alive() is True
    finally:
        _cleanup("/tmp/mmc-local-standalone.conf")


@patch("subprocess.Popen")
def test_pull_process_exits_immediately(mock_popen):
    """When the LS process exits immediately, pull() raises and marks dead."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # exited with error
    mock_popen.return_value = mock_proc

    _write_conf("/tmp/mmc-local-standalone.conf", "")
    try:
        ls = _make_ls(local_service_mode="standalone")
        ls._kv_cfg.local_config_path = "/tmp/mmc-local-inprocess.conf"
        ls.pull()

        assert ls.is_alive() is False
        assert ls.is_started() is False
    finally:
        _cleanup("/tmp/mmc-local-standalone.conf")


def test_stop_kills_process():
    """stop() sends SIGKILL to the LS process."""
    ls = _make_ls(local_service_mode="standalone")
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    ls._ls_process = mock_proc

    with patch("os.kill") as mock_kill:
        ls.stop()
        mock_kill.assert_called_once_with(12345, 9)  # signal.SIGKILL


def test_stop_process_not_found():
    """stop() handles ProcessLookupError gracefully (process already dead)."""
    ls = _make_ls(local_service_mode="standalone")
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    ls._ls_process = mock_proc

    with patch("os.kill", side_effect=ProcessLookupError):
        ls.stop()

    assert ls._ls_process is None
