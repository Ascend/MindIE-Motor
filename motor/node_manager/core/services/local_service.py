# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import re
import subprocess
import threading

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
    ``mmc-local.conf`` is prepared.  In ``standalone`` mode a dedicated
    daemon thread is started via the ``memcache_hybrid`` Python API.
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

        self._endpoints_count: int = 0  # updated by prepare()

        self._ls_thread: threading.Thread | None = None
        self._ls_stop: threading.Event | None = None

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def _worker_count(self) -> int:
        """Total vLLM worker processes on this node."""
        return max(1, self._endpoints_count * self._local_world_size)

    # ------------------------------------------------------------------
    # mmc-local.conf preparation
    # ------------------------------------------------------------------

    def prepare(self, **kwargs) -> None:
        """Modify ``mmc-local.conf`` before engines start (PreparableService protocol).

        Sets ``protocol`` (device_rdma / device_sdma) based on hardware
        type and ``dram.size`` based on local_service_mode and DP count.

        Keyword Args:
            endpoints_count: Number of DP endpoints on this node.
        """
        self._endpoints_count = kwargs.get("endpoints_count", 0)
        if not self._kv_cfg.enable:
            return
        if self._kv_cfg.backend != "memcache":
            return

        conf_path = self._kv_cfg.local_config_path
        if not conf_path or not os.path.exists(conf_path):
            logger.warning("MMC_LOCAL_CONFIG_PATH not found: %s", conf_path)
            return

        ls_mode = self._kv_cfg.local_service_mode

        with open(conf_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Protocol: rdma for A2, sdma for A3/A5
        if self.hardware_type in ("800I_A2", "800T_A2"):
            protocol = "device_rdma"
        else:
            protocol = "device_sdma"

        # dram.size: 0GB for standalone (vLLM uses 0), per-process for inprocess
        if ls_mode == "standalone":
            dram_val = "0GB"
        else:
            per_node = self._kv_cfg.dram_size
            if per_node:
                # User configured — trust it; skip expensive mem_scan.stat()
                configured_gb = self._parse_dram_size_gb(per_node)
                clamped_gb = configured_gb
            else:
                clamped_gb = self._scan_node_available_dram_gb()
            per_process_gb = max(1, clamped_gb // self._worker_count)
            dram_val = f"{per_process_gb}GB"

        content = self._set_conf_key(content, "ock.mmc.local_service.dram.size", dram_val)
        content = self._set_conf_key(content, "ock.mmc.local_service.max.dram.size", "1024GB")
        content = self._set_conf_key(content, "ock.mmc.local_service.protocol", protocol)

        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(
            "Prepared mmc-local.conf: mode=%s, protocol=%s, dram.size=%s, endpoints=%d",
            ls_mode,
            protocol,
            dram_val,
            self._endpoints_count,
        )

    @staticmethod
    def _set_conf_key(content: str, key: str, value: str) -> str:
        """Replace *key* in *content* with *value*; append the line if missing."""
        if not re.search(r'^' + re.escape(key) + r'\s*=', content, flags=re.MULTILINE):
            return content + "\n" + key + " = " + value + "\n"
        return re.sub(
            r'^' + re.escape(key) + r'\s*=\s*.*',
            key + ' = ' + value,
            content,
            flags=re.MULTILINE,
        )

    # ------------------------------------------------------------------
    # standalone LS launch
    # ------------------------------------------------------------------

    def should_launch(self) -> bool:
        if not self._kv_cfg.enable:
            return False
        if self._kv_cfg.backend != "memcache":
            return False
        if self._kv_cfg.local_service_mode != "standalone":
            return False
        return True

    def pull(self) -> None:
        """Start standalone LS in a daemon thread (if not already running).

        Called both after engine spawn (concurrent with warmup) and by the
        process monitor on restart.
        """
        if not self.should_launch():
            return
        if self._ls_thread is not None and self._ls_thread.is_alive():
            return
        self._ls_stop = threading.Event()
        self._ls_thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ls_standalone",
        )
        self._ls_thread.start()

    def stop(self) -> None:
        if self._ls_stop is not None:
            self._ls_stop.set()
        if self._ls_thread is not None and self._ls_thread.is_alive():
            self._ls_thread.join(timeout=2.0)
        self._ls_thread = None
        self._ls_stop = None

    def is_started(self) -> bool:
        return self._ls_thread is not None

    def is_alive(self) -> bool:
        return self._ls_thread is not None and self._ls_thread.is_alive()

    def mark_dead(self) -> None:
        self._ls_thread = None

    def health_check(self) -> None:
        """Check LS thread health; restart if dead (DaemonService protocol)."""
        if self.is_started() and not self.is_alive():
            logger.warning(
                "LocalService thread died (restart_local_service=%s)",
                self.restart_local_service,
            )
            self.mark_dead()
            if self.restart_local_service:
                self.pull()

    def _run(self) -> None:
        """Entry point for the standalone LS daemon thread."""
        try:
            from memcache_hybrid import LocalConfig, DistributedObjectStore  # noqa: PLC0415

            config = LocalConfig()
            # --- MetaService / ConfigStore connectivity ---
            config.meta_service_url = f"tcp://{self._kv_cfg.service}:{self._kv_cfg.port}"
            config.config_store_url = f"tcp://{self._kv_cfg.service}:{self._kv_cfg.config_store_port}"
            # --- General settings ---
            config.log_level = "info"
            config.world_size = 256
            # --- Protocol ---
            if self.hardware_type in ("800I_A2", "800T_A2"):
                config.protocol = "device_rdma"
            else:
                config.protocol = "device_sdma"
            # --- DRAM pool size ---
            per_node = self._kv_cfg.dram_size
            if per_node:
                config.dram_size = per_node
            else:
                config.dram_size = f"{self._scan_node_available_dram_gb()}GB"
            config.max_dram_size = "1024GB"

            logger.info(
                "Starting standalone LocalService (dram=%s, protocol=%s)",
                config.dram_size,
                config.protocol,
            )

            store = DistributedObjectStore()
            res = store.setup(config)
            if res != 0:
                raise RuntimeError("DistributedObjectStore.setup() failed with code %s" % res)
            res = store.init(0)
            if res != 0:
                raise RuntimeError("DistributedObjectStore.init() failed with code %s" % res)

            logger.info("Standalone LocalService initialized successfully")
            self._ls_stop.wait()
        except Exception as e:
            logger.error("Failed to start standalone LocalService: %s", e)
            self._ls_thread = None
            self._ls_stop = None

    # ------------------------------------------------------------------
    # DRAM / memory helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_dram_size_gb(dram_size_str: str) -> int:
        dram_size_str = dram_size_str.strip().upper()
        if dram_size_str.endswith("GB"):
            return int(dram_size_str[:-2])
        raise ValueError("Invalid DRAM size format: '%s'. Expected e.g. '100GB'." % dram_size_str)

    @staticmethod
    def _scan_node_available_dram_gb() -> int:
        """Return available DRAM in GB (floor at 5) using ``free -b``.

        Uses the ``free`` column (truly unused memory) as a conservative
        estimate.  Reserves 20 % for overhead.
        """
        RESERVE_RATIO = 0.8
        MIN_GB = 5

        try:
            proc = subprocess.run(["/usr/bin/free", "-b"], capture_output=True, text=True, timeout=5, check=False)
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("Mem:"):
                        parts = stripped.split()
                        free_bytes = int(parts[3])
                        free_gb = free_bytes // (1024 * 1024 * 1024)
                        if free_gb > 0:
                            result = max(MIN_GB, int(free_gb * RESERVE_RATIO))
                            logger.info(
                                "free -b scan: %d GB free, reserved=%d GB",
                                free_gb,
                                result,
                            )
                            return result
        except Exception:
            logger.warning("Failed to run `free -b` for DRAM scan", exc_info=True)

        logger.warning("Cannot determine available DRAM, using minimum %d GB", MIN_GB)
        return MIN_GB
