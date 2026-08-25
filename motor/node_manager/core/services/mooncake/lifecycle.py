# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Mooncake standalone store lifecycle manager (mirrors memcache LocalService).

Each engine pod runs the official ``mooncake_store_service`` contributing
``global_segment_size``; the process is launched after engines (``pull_kv_store``)
and restarted in place on death, so engines never wait on it.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.common.utils.net import format_address
from motor.config.node_manager import HardwareType, KVCacheStoreConfig
from motor.node_manager.core.services.registry import SERVICE_KV_STORE, register_service

logger = get_logger(__name__)

_DEFAULT_CONF_DIR = "/usr/local/Ascend/Motor/conf"
_STORE_CONFIG_FILENAME = "mooncake_store_config.json"
_STARTUP_GRACE_SEC = 2.0  # catch immediate startup failures after spawn

# Store-process bootstraps (run with ``python -m`` in the store subprocess).
_BOOTSTRAP_ASCEND = "motor.node_manager.core.services.mooncake.bootstrap.ascend_850"
_BOOTSTRAP_ASCEND_800I = "motor.node_manager.core.services.mooncake.bootstrap.ascend_800I"

# HIXL listen port for the store, off the engine's per-device 16666 (verified on 800I, 2026-08-24).
_STORE_HIXL_LISTEN_PORT = "26666"


def _offset_port_range(port_range: str, offset: int) -> str:
    """Shift an ``"A-B"`` port range by ``offset``."""
    try:
        start_s, _, end_s = port_range.partition("-")
        return "%d-%d" % (int(start_s) + offset, int(end_s) + offset)
    except (ValueError, AttributeError):
        return "%d-%d" % (60000 + offset, 60050 + offset)


def _merge_hixl_listen_port(env: dict) -> None:
    """Set the store's HIXL listen port in ASCEND_GLOBAL_RESOURCE_CONFIG.

    HIXL reads the flat dotted key ``comm_resource_config.listen_port`` (official AGRC form).
    Merge, never replace: a pre-set AGRC (e.g. protocol_desc for UBOE) must survive.
    """
    listen_port = env.get("HIXL_LISTEN_PORT", _STORE_HIXL_LISTEN_PORT)
    try:
        agrc = json.loads(env.get("ASCEND_GLOBAL_RESOURCE_CONFIG", "{}"))
    except json.JSONDecodeError:
        agrc = {}
    if not isinstance(agrc, dict):
        agrc = {}
    agrc["comm_resource_config.listen_port"] = listen_port
    env["ASCEND_GLOBAL_RESOURCE_CONFIG"] = json.dumps(agrc)


def _apply_850_store_env(env: dict) -> None:
    """850 store env: move HCCL socket ranges +2000 and the HIXL listen port off the engine's
    16666 (the per-device RA socket 60001 collision otherwise kills the store's channels).
    Explicitly pre-set ranges are respected; only unset ones get the offset store range.
    """
    if "ASCEND_SLOG_PRINT_TO_STDOUT" not in env:
        # Surface device-side (hccp) errors on stdout for log_collect.
        env["ASCEND_SLOG_PRINT_TO_STDOUT"] = "1"
    store_range = _offset_port_range("60000-60050", 2000)
    if "HCCL_HOST_SOCKET_PORT_RANGE" not in env:
        env["HCCL_HOST_SOCKET_PORT_RANGE"] = store_range
    if "HCCL_SOCKET_PORT_RANGE" not in env:
        env["HCCL_SOCKET_PORT_RANGE"] = store_range
    _merge_hixl_listen_port(env)


def _apply_800I_store_env(env: dict) -> None:
    """800I store env: hccp sockets off the workers' 16666 (EI0020 otherwise) via
    HCCL_NPU_SOCKET_PORT_RANGE, HIXL listen port via comm_resource_config.
    """
    if "HCCL_NPU_SOCKET_PORT_RANGE" not in env:
        env["HCCL_NPU_SOCKET_PORT_RANGE"] = "16700-16800"
    _merge_hixl_listen_port(env)


def _create_mooncake_store(hardware_type: str, config):  # pylint: disable=unused-argument
    """MooncakeStoreService factory for the daemon registry."""
    return MooncakeStoreService(
        hardware_type=hardware_type,
        kv_cache_store_config=config.kv_cache_store_config,
        restart_local_service=Env.motor_restart_local_service,
    )


@register_service(
    SERVICE_KV_STORE,
    backend="mooncake",
    prepare_priority=10,
    factory=_create_mooncake_store,
)
class MooncakeStoreService:
    """Manage the standalone ``mooncake_store_service`` subprocess."""

    def __init__(
        self,
        hardware_type: str,
        kv_cache_store_config: KVCacheStoreConfig | None = None,
        restart_local_service: bool = True,
    ):
        self.hardware_type = hardware_type
        self._kv_cfg = kv_cache_store_config or KVCacheStoreConfig()
        self.restart_local_service = restart_local_service

        self._store_process: subprocess.Popen | None = None
        # pull() runs on the API thread while health_check() runs on the monitor
        # thread; the lock serializes them so a race cannot spawn a duplicate
        # store (the loser would orphan a process holding the pool segment).
        self._lock = threading.Lock()
        # Intent flag: the store should be running. health_check() keys on this
        # instead of is_started(), so a failed start is retried next cycle
        # rather than abandoning the store forever.
        self._desired_running = False

    @property
    def _can_launch(self) -> bool:
        return self._kv_cfg.enable and self._kv_cfg.backend == "mooncake" and self._kv_cfg.store_mode == "standalone"

    @staticmethod
    def _conf_dir() -> str:
        """Store config lives next to the engine-side mooncake config."""
        engine_config_path = Env.mooncake_config_path
        if engine_config_path:
            return os.path.dirname(engine_config_path)
        return Env.config_path or _DEFAULT_CONF_DIR

    def _store_config_path(self) -> str:
        return os.path.join(self._conf_dir(), _STORE_CONFIG_FILENAME)

    def _build_store_config(self) -> dict:
        """Same schema as the engine config, but the store contributes the segment and needs no staging buffer."""
        return {
            "local_hostname": Env.pod_ip or "",
            "metadata_server": self._kv_cfg.metadata_server,
            "protocol": self._kv_cfg.protocol,
            "device_name": self._kv_cfg.device_name,
            "global_segment_size": self._kv_cfg.global_segment_size,
            "local_buffer_size": 0,
            "master_server_address": format_address(self._kv_cfg.service, self._kv_cfg.port),
        }

    def _ensure_store_config(self) -> str:
        """(Re)generate the store config file; returns its path or "" on failure."""
        path = self._store_config_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._build_store_config(), f, indent=2)
            return path
        except OSError as e:
            logger.error("Failed to write mooncake store config %s: %s", path, e)
            return ""

    def prepare(self, **kwargs) -> None:
        """Write the store config only — the subprocess is started after engines (pull_kv_store),
        so the engine launch never serializes on the store.
        """
        if not self._kv_cfg.enable or self._kv_cfg.backend != "mooncake":
            return
        if self._kv_cfg.store_mode != "standalone":
            logger.info(
                "Mooncake store_mode=%r (embedded): no standalone store process", self._kv_cfg.store_mode or "embedded"
            )
            return
        if not self._kv_cfg.global_segment_size:
            logger.warning("Mooncake standalone store: global_segment_size is empty, using engine built-in default")
        if self._ensure_store_config():
            logger.info("Mooncake standalone store config prepared")

    def pull(self) -> None:
        """Start the store subprocess (idempotent); called by the start flow
        after engines (pull_kv_store) and by monitor restarts.
        """
        if not self._can_launch:
            return
        with self._lock:
            self._desired_running = True
            self._start_locked()

    def _start_locked(self) -> None:
        """Start the store subprocess if not alive. Caller must hold ``self._lock``."""
        if self.is_alive():
            return

        config_path = self._ensure_store_config()
        if not config_path:
            return

        is_850 = HardwareType.is_a5(self.hardware_type)
        if self._kv_cfg.protocol == "ascend":
            # ACL context must be created inside the store process (does not survive Popen);
            # both bootstraps stay comm-free (see bootstrap modules).
            bootstrap = _BOOTSTRAP_ASCEND if is_850 else _BOOTSTRAP_ASCEND_800I
            cmd = [
                sys.executable,
                "-m",
                bootstrap,
                "--config",
                config_path,
                "--port",
                str(self._kv_cfg.store_http_port),
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "mooncake.mooncake_store_service",
                "--config",
                config_path,
                "--port",
                str(self._kv_cfg.store_http_port),
            ]
        try:
            logger.info(
                "Starting mooncake_store_service (segment=%s, master=%s)",
                self._kv_cfg.global_segment_size,
                format_address(self._kv_cfg.service, self._kv_cfg.port),
            )
            env = os.environ.copy()
            if is_850:
                _apply_850_store_env(env)
            elif self._kv_cfg.protocol == "ascend":
                _apply_800I_store_env(env)
            self._store_process = subprocess.Popen(  # pylint: disable=consider-using-with
                cmd, shell=False, env=env
            )
            # Config or environment problems surface as an immediate exit.
            time.sleep(_STARTUP_GRACE_SEC)
            if self._store_process.poll() is not None:
                raise RuntimeError(
                    "mooncake_store_service exited immediately with code %s" % self._store_process.returncode
                )
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Failed to start mooncake_store_service: %s", e)
            self._store_process = None

    def stop(self) -> None:
        with self._lock:
            self._desired_running = False
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Kill the store process. Caller must hold ``self._lock``."""
        if self._store_process is None:
            return
        pid = self._store_process.pid
        try:
            os.kill(pid, signal.SIGKILL)
            self._store_process.wait(timeout=5.0)
            logger.info("mooncake_store_service terminated (pid=%s)", pid)
        except ProcessLookupError:
            logger.info("mooncake_store_service %s already terminated", pid)
        except subprocess.TimeoutExpired:
            logger.warning("mooncake_store_service %s did not terminate in time", pid)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Failed to kill mooncake_store_service %s: %s", pid, e)
        finally:
            self._store_process = None

    def is_started(self) -> bool:
        return self._store_process is not None

    def is_alive(self) -> bool:
        if self._store_process is None:
            return False
        return self._store_process.poll() is None

    def mark_dead(self) -> None:
        if self._store_process is not None:
            self._store_process.poll()
        self._store_process = None

    def health_check(self) -> None:
        """Restart the store in place when it should be running but is not; the new
        process re-registers and remounts. Keys on the intent flag, so a failed
        start is retried on the next cycle instead of being abandoned.
        """
        with self._lock:
            if not self._desired_running or self.is_alive():
                return
            logger.warning(
                "mooncake_store_service is not running (restart_local_service=%s)",
                self.restart_local_service,
            )
            self.mark_dead()
            if self.restart_local_service:
                self._start_locked()
