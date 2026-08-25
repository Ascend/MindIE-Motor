# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for the Mooncake standalone store daemon service."""

import json
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from motor.config.node_manager import HardwareType, KVCacheStoreConfig
from motor.node_manager.core.services.mooncake.lifecycle import (
    MooncakeStoreService,
    _BOOTSTRAP_ASCEND,
    _BOOTSTRAP_ASCEND_800I,
)

_850_HW = HardwareType.TYPE_950_SUPERPOD_ATLAS_8.value
_800I_HW = HardwareType.TYPE_800I_A2.value

_MODULE = "motor.node_manager.core.services.mooncake.lifecycle"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_store(**overrides) -> MooncakeStoreService:
    """Quick one-off MooncakeStoreService with overrides on top of defaults."""
    cfg = KVCacheStoreConfig()
    cfg.enable = True
    cfg.backend = "mooncake"
    cfg.store_mode = "standalone"
    cfg.service = "kvs-master"
    cfg.port = 50088
    cfg.global_segment_size = "600GB"
    hw = overrides.pop("hardware_type", _800I_HW)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return MooncakeStoreService(
        hardware_type=hw,
        kv_cache_store_config=cfg,
    )


def _alive_proc() -> MagicMock:
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


# ===================================================================
# launch gating — pull() is a no-op unless standalone mode is active
# ===================================================================


@patch("subprocess.Popen")
@pytest.mark.parametrize(
    "overrides",
    [
        {"store_mode": "embedded"},
        {"store_mode": ""},
        {"enable": False},
        {"backend": "memcache"},
    ],
    ids=["embedded", "default_mode", "disabled", "wrong_backend"],
)
def test_pull_skipped_unless_standalone(mock_popen, overrides):
    """prepare()+pull() launch nothing unless store_mode == "standalone"."""
    store = _make_store(**overrides)
    store.prepare()
    store.pull()
    mock_popen.assert_not_called()


@patch("subprocess.Popen")
def test_prepare_does_not_launch_store(mock_popen, tmp_path, monkeypatch):
    """prepare() only writes the store config; the store process must be
    started after engines (pull_kv_store), never during engine start.
    """
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    store = _make_store()
    store.prepare()

    mock_popen.assert_not_called()
    assert store.is_started() is False
    # config file was written for the later pull_kv_store launch
    generated = json.loads((tmp_path / "mooncake_store_config.json").read_text(encoding="utf-8"))
    assert generated["protocol"] == "ascend"
    assert generated["global_segment_size"] == "600GB"


# ===================================================================
# pull() — standalone store (Popen)
# ===================================================================


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_launches_official_store_service(mock_popen, _sleep, tmp_path, monkeypatch):
    """pull() runs the official store via the 850 bootstrap module (python -m),
    which sets up the ACL device context + HCCL comm first (the standalone
    store process never calls aclrtSetDevice itself, so AscendDirectTransport
    would fail with "cannot allocate local segment, ret: -1").
    """
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    store = _make_store(hardware_type=_850_HW)
    store.pull()

    mock_popen.assert_called_once()
    call_args, call_kwargs = mock_popen.call_args
    cmd = call_args[0]
    assert cmd[0] == sys.executable
    assert cmd[1] == "-m"
    assert cmd[2] == _BOOTSTRAP_ASCEND
    assert cmd[3:] == ["--config", str(tmp_path / "mooncake_store_config.json"), "--port", "0"]
    assert call_kwargs["shell"] is False
    assert store.is_alive() is True


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_injects_hixl_listen_port(mock_popen, _sleep, tmp_path, monkeypatch):
    """ascend protocol: point HIXL's device NIC listen port away from 16666
    (which vllm workers bind first) so the standalone store can build its
    channel. A pre-set ASCEND_GLOBAL_RESOURCE_CONFIG is MERGED, not replaced —
    the pod may ship comm_resource_config.protocol_desc (UBOE/UB), and
    dropping it would silently lose the listen_port override and put the store
    back on the engine's 16666.
    """
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    _make_store().pull()
    env = mock_popen.call_args.kwargs["env"]
    assert env["ASCEND_GLOBAL_RESOURCE_CONFIG"] == '{"comm_resource_config.listen_port": "26666"}'

    monkeypatch.setenv("HIXL_LISTEN_PORT", "30000")
    _make_store().pull()
    env = mock_popen.call_args.kwargs["env"]
    assert env["ASCEND_GLOBAL_RESOURCE_CONFIG"] == '{"comm_resource_config.listen_port": "30000"}'

    # user pre-set AGRC keeps its own keys; listen_port is merged in
    monkeypatch.setenv("ASCEND_GLOBAL_RESOURCE_CONFIG", '{"fabric_memory.max_capacity": "64"}')
    monkeypatch.delenv("HIXL_LISTEN_PORT", raising=False)
    _make_store().pull()
    env = mock_popen.call_args.kwargs["env"]
    assert env["ASCEND_GLOBAL_RESOURCE_CONFIG"] == (
        '{"fabric_memory.max_capacity": "64", "comm_resource_config.listen_port": "26666"}'
    )

    # pre-set comm_resource_config.* keys (official dotted form, e.g. UBOE)
    # survive the merge
    monkeypatch.setenv(
        "ASCEND_GLOBAL_RESOURCE_CONFIG",
        '{"comm_resource_config.protocol_desc": ["uboe:device"]}',
    )
    _make_store().pull()
    env = mock_popen.call_args.kwargs["env"]
    assert env["ASCEND_GLOBAL_RESOURCE_CONFIG"] == (
        '{"comm_resource_config.protocol_desc": ["uboe:device"], "comm_resource_config.listen_port": "26666"}'
    )


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
@pytest.mark.parametrize("hw", [_800I_HW, HardwareType.TYPE_800I_A3.value], ids=["800I-A2", "800I-A3"])
def test_pull_800I_acl_context_without_comm(mock_popen, _sleep, tmp_path, monkeypatch, hw):
    """800I: the store bootstrap provides ONLY the ACL context — no HCCL comm.
    A comm would make the store's adxl engine merge its own root info into the
    P2P handshake; on a same-node PD deployment the store shares physical
    devices with the vllm workers, so the merged channel rank table repeats a
    device IP and HCCL rejects it with EI0014 (TP0 fails every put, TP1
    succeeds) — the comm-free handshake is TP-agnostic. The store's hccp
    sockets still move off the workers' 16666 (EI0020 otherwise).
    """
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    _make_store(hardware_type=hw).pull()

    call_args, call_kwargs = mock_popen.call_args
    cmd = call_args[0]
    assert cmd[1] == "-m"
    assert cmd[2] == _BOOTSTRAP_ASCEND_800I

    env = call_kwargs["env"]
    assert env["HCCL_NPU_SOCKET_PORT_RANGE"] == "16700-16800"
    assert env["ASCEND_GLOBAL_RESOURCE_CONFIG"] == '{"comm_resource_config.listen_port": "26666"}'


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_injects_slog_print_to_stdout(mock_popen, _sleep, tmp_path, monkeypatch):
    """store device-side (hccp) errors are surfaced on the store stdout so
    log_collect keeps them (they would otherwise go to the host slog, which
    the pod cannot read); a pre-set ASCEND_SLOG_PRINT_TO_STDOUT wins.
    """
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    _make_store(hardware_type=_850_HW).pull()
    env = mock_popen.call_args.kwargs["env"]
    assert env["ASCEND_SLOG_PRINT_TO_STDOUT"] == "1"

    monkeypatch.setenv("ASCEND_SLOG_PRINT_TO_STDOUT", "0")
    _make_store(hardware_type=_850_HW).pull()
    env = mock_popen.call_args.kwargs["env"]
    assert env["ASCEND_SLOG_PRINT_TO_STDOUT"] == "0"


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_850_respects_preset_socket_ranges(mock_popen, _sleep, tmp_path, monkeypatch):
    """850: an explicitly pre-set HCCL_(HOST_)SOCKET_PORT_RANGE is the user's
    deliberate choice and must survive; only unset ones get the +2000 store range.
    """
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.setenv("POD_IP", "10.0.0.1")
    monkeypatch.setenv("HCCL_SOCKET_PORT_RANGE", "50000-50050")

    _make_store(hardware_type=_850_HW).pull()
    env = mock_popen.call_args.kwargs["env"]
    assert env["HCCL_SOCKET_PORT_RANGE"] == "50000-50050"
    assert env["HCCL_HOST_SOCKET_PORT_RANGE"] == "62000-62050"


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_non_ascend_protocol_uses_plain_module(mock_popen, _sleep, tmp_path, monkeypatch):
    """Non-ascend protocols keep the plain ``python -m`` launch (no ACL needed)."""
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))

    _make_store(protocol="tcp").pull()

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd[:3] == [sys.executable, "-m", "mooncake.mooncake_store_service"]
    assert cmd[3:] == ["--config", str(tmp_path / "mooncake_store_config.json"), "--port", "0"]


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_writes_store_config(mock_popen, _sleep, tmp_path, monkeypatch):
    """pull() generates the store config: segment from cfg, no staging buffer."""
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    _make_store(store_http_port=9090).pull()

    generated = json.loads((tmp_path / "mooncake_store_config.json").read_text(encoding="utf-8"))
    assert generated["global_segment_size"] == "600GB"
    assert generated["local_buffer_size"] == 0
    assert generated["master_server_address"] == "kvs-master:50088"
    assert generated["local_hostname"] == "10.0.0.1"
    assert generated["metadata_server"] == "P2PHANDSHAKE"
    assert generated["protocol"] == "ascend"
    # store_http_port reached the command line, not the config file
    assert mock_popen.call_args[0][0][-1] == "9090"


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_brackets_ipv6_master(mock_popen, _sleep, tmp_path, monkeypatch):
    """IPv6 master service FQDN is bracketed in master_server_address."""
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    monkeypatch.delenv("POD_IP", raising=False)

    _make_store(service="2001:db8::1").pull()

    generated = json.loads((tmp_path / "mooncake_store_config.json").read_text(encoding="utf-8"))
    assert generated["master_server_address"] == "[2001:db8::1]:50088"


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_idempotent_when_alive(mock_popen, _sleep, tmp_path, monkeypatch):
    """Second pull() is a no-op while the store is running."""
    mock_popen.return_value = _alive_proc()
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))

    store = _make_store()
    store.pull()
    store.pull()

    assert mock_popen.call_count == 1


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_pull_process_exits_immediately(mock_popen, _sleep, tmp_path, monkeypatch):
    """Immediate startup failure is detected and the store stays unstarted."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1
    mock_popen.return_value = mock_proc
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))

    store = _make_store()
    store.pull()

    assert store.is_alive() is False
    assert store.is_started() is False


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_health_check_retries_after_start_failure(mock_popen, _sleep, tmp_path, monkeypatch):
    """A failed start (is_started()=False) must NOT abandon the store: the intent
    flag survives, so the next health_check retries the launch. Otherwise one
    transient failure (config dir briefly unwritable, port briefly taken) would
    permanently disable pooling, contradicting restart-in-place.
    """
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1
    mock_popen.side_effect = [dead_proc, _alive_proc()]

    store = _make_store()
    store.pull()
    assert store.is_started() is False

    store.health_check()

    assert mock_popen.call_count == 2
    assert store.is_alive() is True


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_health_check_no_restart_after_stop(mock_popen, _sleep, tmp_path, monkeypatch):
    """stop() clears the intent flag: health_check must not resurrect the store."""
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    mock_popen.return_value = _alive_proc()

    store = _make_store()
    store.pull()
    store.stop()
    store.health_check()

    assert mock_popen.call_count == 1


@patch(f"{_MODULE}.time")
@patch("subprocess.Popen")
def test_concurrent_pull_starts_single_store(mock_popen, _time, tmp_path, monkeypatch):
    """pull() runs on the API thread while health_check() runs on the monitor
    thread; a race must not spawn two stores — the loser would orphan a process
    holding the pool segment, never health-checked and never reaped.
    """
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    entered = threading.Event()
    release = threading.Event()

    def _slow_popen(*args, **kwargs):
        entered.set()
        release.wait(5)
        return _alive_proc()

    mock_popen.side_effect = _slow_popen

    store = _make_store()
    racer = threading.Thread(target=store.pull)
    racer.start()
    assert entered.wait(5)
    store.pull()  # main thread races while the first Popen is still in flight
    release.set()
    racer.join(timeout=5)

    assert mock_popen.call_count == 1
    assert store.is_alive() is True


# ===================================================================
# stop()
# ===================================================================


def test_stop_kills_process():
    store = _make_store()
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    store._store_process = mock_proc

    with patch("os.kill") as mock_kill:
        store.stop()
        mock_kill.assert_called_once_with(12345, 9)  # signal.SIGKILL


def test_stop_process_not_found():
    store = _make_store()
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    store._store_process = mock_proc

    with patch("os.kill", side_effect=ProcessLookupError):
        store.stop()

    assert store._store_process is None


# ===================================================================
# health_check() — restart in place
# ===================================================================


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_health_check_restarts_dead_store(mock_popen, _sleep, tmp_path, monkeypatch):
    """Dead store is re-pulled in place when restart is enabled."""
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    proc1, proc2 = _alive_proc(), _alive_proc()
    mock_popen.side_effect = [proc1, proc2]

    store = _make_store()
    store.pull()
    assert mock_popen.call_count == 1

    # Store dies
    proc1.poll.return_value = 1
    store.health_check()

    assert mock_popen.call_count == 2
    assert store._store_process is proc2
    assert store.is_alive() is True


@patch(f"{_MODULE}.time.sleep")
@patch("subprocess.Popen")
def test_health_check_no_restart_when_disabled(mock_popen, _sleep, tmp_path, monkeypatch):
    """MOTOR_RESTART_LOCAL_SERVICE=0 semantics: dead store stays dead."""
    monkeypatch.setenv("MOONCAKE_CONFIG_PATH", str(tmp_path / "kv_cache_store_config.json"))
    mock_popen.return_value = _alive_proc()

    store = _make_store()
    store.restart_local_service = False
    store.pull()

    store._store_process.poll.return_value = 1
    store.health_check()

    assert mock_popen.call_count == 1
    assert store.is_started() is False
