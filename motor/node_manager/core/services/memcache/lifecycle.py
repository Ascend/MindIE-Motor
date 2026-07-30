# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Daemon-side lifecycle manager for memcache LocalService.

The *actual* LocalService worker runs in a subprocess; its entry point is
:mod:`motor.node_manager.core.services.memcache.worker`.  This module only
handles the daemon-side lifecycle (start / stop / health_check) and is
registered with the daemon via ``@register_service``.
"""

import os
import signal
import subprocess
import sys

from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.config.node_manager import KVCacheStoreConfig
from motor.node_manager.core.services.registry import register_service, SERVICE_KV_STORE

logger = get_logger(__name__)


def _create_local_service(hardware_type: str, config):  # pylint: disable=unused-argument
    """Factory for LocalService — keeps constructor details out of the daemon."""
    return LocalService(
        hardware_type=hardware_type,
        kv_cache_store_config=config.kv_cache_store_config,
        local_world_size=config.basic_config.parallel_config.local_world_size,
        restart_local_service=Env.motor_restart_local_service,
    )


@register_service(
    SERVICE_KV_STORE,
    backend="memcache",
    prepare_priority=10,
    factory=_create_local_service,
)
class LocalService:
    """Manage memcache LocalService lifecycle: conf, start, health-check, restart.

    In ``inprocess`` mode the LocalService runs inside vLLM — only the
    ``mmc-local-inprocess.conf`` is prepared.  In ``standalone`` mode a
    dedicated subprocess is started via ``sys.executable -m``, mirroring
    how engine processes are launched.
    """

    def __init__(
        self,
        hardware_type: str,
        kv_cache_store_config: KVCacheStoreConfig | None = None,
        local_world_size: int = 1,
        restart_local_service: bool = True,
    ):
        self.hardware_type = hardware_type
        self._kv_cfg = kv_cache_store_config or KVCacheStoreConfig()
        self._local_world_size = local_world_size
        self.restart_local_service = restart_local_service

        self._ls_process: subprocess.Popen | None = None

    # ------------------------------------------------------------------
    # conditional launch
    # ------------------------------------------------------------------

    @property
    def _can_launch(self) -> bool:
        """True when this service should manage a standalone LS subprocess."""
        return (
            self._kv_cfg.enable
            and self._kv_cfg.backend == "memcache"
            and self._kv_cfg.local_service_mode == "standalone"
        )

    def should_launch(self) -> bool:
        return self._can_launch

    # ------------------------------------------------------------------
    # mmc-local-inprocess.conf preparation
    # ------------------------------------------------------------------

    def prepare(self, **kwargs) -> None:
        """Ensure mmc-local-inprocess.conf exists and enforce hardware-mode constraints.

        Does NOT modify conf file content — all memcache settings are
        user-managed in mmc-local-inprocess.conf.

        Keyword Args:
            endpoints_count: Number of DP endpoints on this node (informational only).
        """
        if not self._kv_cfg.enable:
            return
        if self._kv_cfg.backend != "memcache":
            return

        conf_path = self._kv_cfg.local_config_path
        if not conf_path or not os.path.exists(conf_path):
            logger.warning("MMC_LOCAL_CONFIG_PATH not found: %s", conf_path)
            return

        ls_mode = self._kv_cfg.local_service_mode

        # A2 / A5 only support inprocess; override standalone if configured
        if self.hardware_type not in ("800I_A3", "800T_A3"):
            if ls_mode not in ("", "inprocess"):
                logger.warning(
                    "Hardware %s does not support standalone mode; forcing inprocess (configured: %s)",
                    self.hardware_type,
                    ls_mode,
                )
            ls_mode = "inprocess"
            self._kv_cfg.local_service_mode = "inprocess"

        # vLLM workers read MMC_LOCAL_CONFIG_PATH to locate their conf file.
        os.environ["MMC_LOCAL_CONFIG_PATH"] = conf_path

        endpoints_count = kwargs.get("endpoints_count", 0)
        logger.info(
            "Prepared mmc-local-inprocess.conf: mode=%s, path=%s, endpoints=%d",
            ls_mode,
            conf_path,
            endpoints_count,
        )

    # ------------------------------------------------------------------
    # standalone LS launch (subprocess)
    # ------------------------------------------------------------------

    def pull(self) -> None:
        """Start standalone LS as a subprocess (if not already running).

        Uses ``sys.executable -m`` to launch the worker entry point in
        :mod:`motor.node_manager.core.services.memcache.worker`.  Each
        subprocess gets its own env dict (``subprocess.Popen(env=...)``)
        so the standalone conf path does not leak to sibling processes
        (e.g. engine subprocesses that need the inprocess conf path).

        Called both after engine spawn (concurrent with warmup) and by the
        process monitor on restart.
        """
        if not self._can_launch:
            return
        if self.is_alive():
            return

        try:
            # Switch MMC_LOCAL_CONFIG_PATH to the standalone conf so the
            # subprocess picks up standalone-specific settings.
            conf_dir = os.path.dirname(self._kv_cfg.local_config_path)
            standalone_path = os.path.join(conf_dir, "mmc-local-standalone.conf")
            if not os.path.exists(standalone_path):
                logger.error("Standalone conf not found: %s", standalone_path)
                return

            env = os.environ.copy()
            env["MMC_LOCAL_CONFIG_PATH"] = standalone_path

            cmd = [sys.executable, "-m", "motor.node_manager.core.services.memcache.worker"]
            logger.info("Starting standalone LocalService")
            self._ls_process = subprocess.Popen(cmd, shell=False, env=env)  # pylint: disable=consider-using-with
            if self._ls_process.poll() is not None:
                raise RuntimeError("LocalService process exited immediately with code %s" % self._ls_process.returncode)
        except Exception as e:
            logger.error("Failed to start standalone LocalService: %s", e)
            self._ls_process = None

    def stop(self) -> None:
        if self._ls_process is None:
            return
        pid = self._ls_process.pid
        try:
            os.kill(pid, signal.SIGKILL)
            self._ls_process.wait(timeout=5.0)
            logger.info("LocalService process terminated (pid=%s)", pid)
        except ProcessLookupError:
            logger.info("LocalService process %s already terminated", pid)
        except subprocess.TimeoutExpired:
            logger.warning("LocalService process %s did not terminate in time", pid)
        except Exception as e:
            logger.error("Failed to kill LocalService process %s: %s", pid, e)
        finally:
            self._ls_process = None

    def is_started(self) -> bool:
        return self._ls_process is not None

    def is_alive(self) -> bool:
        if self._ls_process is None:
            return False
        return self._ls_process.poll() is None

    def mark_dead(self) -> None:
        """Non-blocking check and reap of the LS subprocess."""
        if self._ls_process is not None:
            self._ls_process.poll()
        self._ls_process = None

    def health_check(self) -> None:
        """Check LS process health; restart if dead (DaemonService protocol)."""
        if self.is_started() and not self.is_alive():
            logger.warning(
                "LocalService process died (restart_local_service=%s)",
                self.restart_local_service,
            )
            self.mark_dead()
            if self.restart_local_service:
                self.pull()
