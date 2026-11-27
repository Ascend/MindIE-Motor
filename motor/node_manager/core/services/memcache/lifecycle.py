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
import tempfile
from contextlib import nullcontext
from pathlib import Path

from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.config.node_manager import KVCacheStoreConfig
from motor.node_manager.core.services.registry import register_service, SERVICE_KV_STORE

logger = get_logger(__name__)


def _read_exit_plan(payload: dict, *, timeout: float = 10) -> dict:
    from motor.node_manager.api_client.controller_api_client import ControllerApiClient

    return ControllerApiClient.plan_provider_exit(payload, timeout=timeout)


def _create_local_service(hardware_type: str, config):  # pylint: disable=unused-argument
    """Factory for LocalService — keeps constructor details out of the daemon."""
    return LocalService(
        hardware_type=hardware_type,
        kv_cache_store_config=config.kv_cache_store_config,
        local_world_size=config.basic_config.parallel_config.local_world_size,
        restart_local_service=Env.motor_restart_local_service,
        kv_migration_enabled=config.fault_tolerance_config.enable_kv_migration,
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
        kv_migration_enabled: bool = False,
    ):
        self.hardware_type = hardware_type
        self._kv_cfg = kv_cache_store_config or KVCacheStoreConfig()
        self._local_world_size = local_world_size
        self.restart_local_service = restart_local_service
        self._ls_process: subprocess.Popen | None = None
        self._ls_rank_directory: tempfile.TemporaryDirectory | None = None
        self.migration = None
        if kv_migration_enabled:
            from motor.node_manager.core.services.memcache.migration import MemcacheMigration

            self.migration = MemcacheMigration(
                os.environ.get("MMC_DRAIN_API_URL", "").rstrip("/"),
                self._live_storage_rank,
                _read_exit_plan,
                lambda: getattr(self._ls_rank_directory, "name", None),
            )

    def _live_storage_rank(self) -> tuple[subprocess.Popen, int]:
        process = self._ls_process
        directory = self._ls_rank_directory
        if process is None or process.poll() is not None:
            raise RuntimeError("Cannot identify a live standalone source Pod")
        if directory is None:
            raise RuntimeError("Missing standalone storage rank")
        try:
            pid, rank = map(int, (Path(directory.name) / "rank").read_text(encoding="ascii").split())
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError("Missing or invalid standalone storage rank") from exc
        if pid != process.pid or not 0 <= rank < (1 << 32) - 1:
            raise RuntimeError("Invalid standalone storage rank for the running worker")
        return process, rank

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

    @staticmethod
    def _set_node_label(conf_path: str, node_label: str | None) -> None:
        """Set the storage provider's drain selector, or remove it from a client config."""
        if not os.path.exists(conf_path):
            return
        if node_label is not None and (not node_label or any(char in node_label for char in "\r\n=")):
            raise ValueError("MMC_LOCAL_SERVICE_NODE_LABEL contains invalid characters")
        # Memcache LocalService configuration uses the ock.mmc.local_service namespace.
        key = "ock.mmc.local_service.node_label"
        with open(conf_path, "r", encoding="utf-8") as conf_file:
            lines = conf_file.readlines()
        output = [line for line in lines if line.partition("=")[0].strip() != key]
        if node_label is not None:
            if output and not output[-1].endswith("\n"):
                output[-1] += "\n"
            output.append(f"{key} = {node_label}\n")
        if output == lines:
            return
        with open(conf_path, "w", encoding="utf-8") as conf_file:
            conf_file.writelines(output)

    def _prepare_drain_label(self, conf_path: str) -> None:
        provider_path = os.path.join(os.path.dirname(conf_path), "mmc-local-standalone.conf")
        if os.path.exists(provider_path) and os.path.samefile(conf_path, provider_path):
            raise ValueError("Standalone Memcache storage and engine clients require separate config files")
        self._set_node_label(conf_path, None)
        label = os.environ.get("MMC_LOCAL_SERVICE_NODE_LABEL") or os.environ.get("POD_IP")
        if label:
            self._set_node_label(provider_path, label)

    def prepare(self, **kwargs) -> None:
        """Select the engine config and configure the drain label on the storage provider.

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

        if self.migration is not None and ls_mode == "standalone":
            self._prepare_drain_label(conf_path)

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
        rank_directory = None
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
            with self.migration.submission_guard() if self.migration is not None else nullcontext():
                if self.migration is not None:
                    if self.is_alive():
                        return
                    rank_directory = tempfile.TemporaryDirectory(  # pylint: disable=consider-using-with
                        prefix="motor-memcache-"
                    )
                    cmd.append(str(Path(rank_directory.name) / "rank"))
                logger.info("Starting standalone LocalService")
                process = subprocess.Popen(cmd, shell=False, env=env)  # pylint: disable=consider-using-with
                if process.poll() is not None:
                    raise RuntimeError("LocalService process exited immediately with code %s" % process.returncode)
                self._ls_rank_directory = rank_directory
                self._ls_process = process
        except Exception as e:
            logger.error("Failed to start standalone LocalService: %s", e)
            if not self.is_alive():
                self._ls_process = None
            if rank_directory is not None:
                rank_directory.cleanup()

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
            self._clear_rank_directory()

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
        self._clear_rank_directory()

    def _clear_rank_directory(self) -> None:
        if self._ls_rank_directory is not None:
            # Its name still identifies the last worker's journal until a replacement starts.
            self._ls_rank_directory.cleanup()

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
