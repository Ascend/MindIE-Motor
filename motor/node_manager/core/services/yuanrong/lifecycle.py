# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Yuanrong Worker lifecycle managed by NodeManager (MemCache LocalService analogue).

Yuanrong is distributed: every P/D node runs one Worker.  ETCD remains external
(deployed separately).  This service starts / health-checks / restarts the local
Worker via ``dscli start``, similar to how LocalService manages standalone
MemCache LocalService.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time

from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.common.utils.net import format_address
from motor.config.node_manager import (
    KVCacheStoreConfig,
    _validate_yuanrong_args,
    _validate_yuanrong_etcd_address,
)
from motor.node_manager.core.services.registry import SERVICE_KV_STORE, register_service

logger = get_logger(__name__)

# Fallback root for the Worker work dir / client logs when MOTOR_LOG_PATH is unset.
DEFAULT_LOG_ROOT = "/root/ascend/log"

# Restart backoff after a failed health_check restart: 10s * 2^(n-1), capped at 5 min.
RESTART_BACKOFF_BASE_S = 10
RESTART_BACKOFF_MAX_S = 300
WORKER_READY_TIMEOUT_S = 30
WORKER_STOP_TIMEOUT_S = 30
WORKER_KILL_TIMEOUT_S = 5


def _create_yuanrong_service(hardware_type: str, config):  # pylint: disable=unused-argument
    """Factory for YuanrongService — keeps constructor details out of the daemon."""
    return YuanrongService(
        kv_cache_store_config=config.kv_cache_store_config,
        restart_worker=Env.motor_restart_local_service,
    )


@register_service(
    SERVICE_KV_STORE,
    backend="yuanrong",
    prepare_priority=10,
    factory=_create_yuanrong_service,
)
class YuanrongService:
    """Manage Yuanrong Worker lifecycle on each P/D node.

    Mirrors MemCache LocalService:
    - ``prepare()``: ensure dirs / client env, start Worker before engines
    - ``pull()``: idempotent start (also used after engine spawn / restart)
    - ``health_check()``: detect death and self-restart
    - ``stop()``: stop the daemonized Worker through ``dscli``
    """

    def __init__(
        self,
        kv_cache_store_config: KVCacheStoreConfig | None = None,
        restart_worker: bool = True,
    ):
        self._kv_cfg = kv_cache_store_config or KVCacheStoreConfig()
        self.restart_worker = restart_worker
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._started = False
        self._ready_deadline: float | None = None
        self._restart_failures = 0
        self._next_restart_ts = 0.0

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------

    @property
    def _host_ip(self) -> str:
        return os.getenv("POD_IP") or os.getenv("HOST_IP") or "127.0.0.1"

    @property
    def _worker_port(self) -> int:
        worker_port = self._kv_cfg.worker_port
        if not 1 <= worker_port <= 65535:
            raise RuntimeError("kv_cache_store_config.worker_port is required for the yuanrong backend")
        return worker_port

    @property
    def _worker_address(self) -> str:
        return format_address(self._host_ip, self._worker_port)

    @property
    def _shared_memory_size_mb(self) -> int:
        shared_memory_size_mb = self._kv_cfg.shared_memory_size_mb
        if shared_memory_size_mb <= 0:
            raise RuntimeError("kv_cache_store_config.shared_memory_size_mb is required for the yuanrong backend")
        return shared_memory_size_mb

    @property
    def _etcd_address(self) -> str:
        return _validate_yuanrong_etcd_address(
            os.getenv("ETCD_ADDRESS", "").strip() or self._kv_cfg.etcd_address.strip()
        )

    @property
    def _log_root(self) -> str:
        """Mounted dir holding this pod's Yuanrong files.

        Derived from ``MOTOR_LOG_PATH`` — the directory the deployer already
        mounts as a hostPath for Motor's own logs, so the mount is known-good
        and the files outlive the pod.  Its parent makes ``yuanrong/`` a sibling
        of ``motor/``, keeping two Motor deployments on one node apart.

        When ``MOTOR_LOG_PATH`` is unset (e.g. SINGLE_CONTAINER mode), fall
        back to ``MOTOR_LOG_ROOT_PATH`` with ``<model>/<service_id>`` inserted.
        """
        motor_log_path = os.getenv("MOTOR_LOG_PATH", "")
        if motor_log_path:
            return os.path.join(os.path.dirname(motor_log_path), "yuanrong")
        log_root = os.getenv("MOTOR_LOG_ROOT_PATH", "") or DEFAULT_LOG_ROOT
        model = os.getenv("MODEL_NAME", "")
        svc_id = os.getenv("SERVICE_ID", "")
        if model and svc_id:
            return os.path.join(log_root, model, svc_id, "yuanrong")
        return os.path.join(log_root, "yuanrong")

    @property
    def _role_home(self) -> str:
        role = os.getenv("ROLE", "worker")
        return os.path.join(self._log_root, role)

    def _export_client_env(self) -> None:
        """Export client-side env so engine subprocesses inherit Worker address."""
        os.environ["DS_WORKER_ADDR"] = self._worker_address
        os.environ["ETCD_ADDRESS"] = self._etcd_address
        os.environ.setdefault("DATASYSTEM_LOG_ONLY_WRITE_INFO_FILE", "false")
        os.environ.pop("GOOGLE_LOGTOSTDERR", None)
        os.environ.pop("GOOGLE_ALSOLOGTOSTDERR", None)

        client_log = os.path.join(self._role_home, "client")
        os.makedirs(client_log, exist_ok=True)
        os.environ.setdefault("DATASYSTEM_CLIENT_LOG_DIR", client_log)

        ld_path = os.environ.get("LD_LIBRARY_PATH", "")
        if "/usr/local/lib" not in ld_path.split(":"):
            os.environ["LD_LIBRARY_PATH"] = f"/usr/local/lib:{ld_path}" if ld_path else "/usr/local/lib"

    def _build_cmd(self) -> list[str]:
        """Build ``dscli start <start_args> -w <managed args> <worker_args>``."""
        etcd_address = self._etcd_address
        launcher = self._kv_cfg.worker_launcher
        cmd = ["dscli", "start"]
        cmd.extend(self._expand_args(launcher.start_args, "worker_launcher.start_args"))
        cmd.extend(
            [
                "-w",
                "--worker_address",
                self._worker_address,
                "--etcd_address",
                etcd_address,
                "--shared_memory_size_mb",
                str(self._shared_memory_size_mb),
            ]
        )
        cmd.extend(self._expand_args(launcher.worker_args, "worker_launcher.worker_args"))
        return cmd

    @staticmethod
    def _expand_args(records: object, field_name: str) -> list[str]:
        """Expand ordered one-entry objects into CLI argv tokens."""
        validated = _validate_yuanrong_args(records, f"kv_cache_store_config.{field_name}")
        tokens: list[str] = []
        for record in validated:
            arg_name, arg_value = next(iter(record.items()))
            tokens.append(f"-{arg_name}" if len(arg_name) == 1 else f"--{arg_name}")
            if arg_value is None:
                continue
            tokens.append(YuanrongService._stringify_arg_value(arg_value))
        return tokens

    @staticmethod
    def _stringify_arg_value(value: object) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)

    def _is_port_open(self) -> bool:
        try:
            with socket.create_connection((self._host_ip, self._worker_port), timeout=1.0):
                return True
        except OSError:
            return False

    def _worker_process_exists(self) -> bool:
        """Check for a Worker process serving this service's exact address."""
        return bool(self._worker_pids())

    def _worker_pids(self) -> list[int]:
        """Find only Workers serving this service's exact address."""
        pgrep_path = shutil.which("pgrep")
        if pgrep_path is None:
            raise RuntimeError("Failed to inspect Yuanrong Worker processes: pgrep not found on PATH; install procps")
        try:
            result = subprocess.run(
                [os.path.abspath(pgrep_path), "-af", "datasystem_worker"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Failed to inspect Yuanrong Worker processes") from exc
        if result.returncode == 1:
            return []
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to inspect Yuanrong Worker processes "
                f"(returncode={result.returncode}, stderr={result.stderr.strip()})"
            )

        pids = []
        for line in result.stdout.splitlines():
            pid_text, separator, command = line.partition(" ")
            if separator and pid_text.isdigit() and int(pid_text) > 0 and self._command_matches_worker(command):
                pids.append(int(pid_text))
        return pids

    def _worker_pid_matches(self, pid: int) -> bool:
        """Recheck the current command line before signaling or waiting on a PID."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as command_file:
                argv = command_file.read().decode().rstrip("\0").split("\0")
        except (FileNotFoundError, ProcessLookupError):
            return False
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"Failed to inspect Yuanrong Worker pid={pid}") from exc
        return self._command_matches_worker(shlex.join(argv))

    def _kill_worker_processes(self) -> None:
        """Force-stop matching Workers and wait a bounded time for their exit."""
        pids = self._worker_pids()
        for pid in pids:
            try:
                if self._worker_pid_matches(pid):
                    logger.warning("Force-killing Yuanrong Worker pid=%s at %s", pid, self._worker_address)
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue  # Worker exited between inspection and signaling.
            except OSError as exc:
                raise RuntimeError(f"Failed to kill Yuanrong Worker pid={pid}") from exc
        deadline = time.monotonic() + WORKER_KILL_TIMEOUT_S
        while any(self._worker_pid_matches(pid) for pid in pids):
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for Yuanrong Worker to exit after SIGKILL")
            time.sleep(0.1)

    def _command_matches_worker(self, command: str) -> bool:
        """Check an argv string for this exact ``datasystem_worker`` address."""
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        if not argv or os.path.basename(argv[0]) != "datasystem_worker":
            return False
        for index, token in enumerate(argv):
            normalized = token.lstrip("-")
            if normalized == f"worker_address={self._worker_address}":
                return True
            if normalized == "worker_address" and index + 1 < len(argv):
                return argv[index + 1] == self._worker_address
        return False

    def _worker_ready(self) -> bool:
        """Require both the expected Worker process and its listening port."""
        return self._is_port_open() and self._worker_process_exists()

    # ------------------------------------------------------------------
    # DaemonService / PreparableService
    # ------------------------------------------------------------------

    def prepare(self, **kwargs) -> None:  # pylint: disable=unused-argument
        """Prepare client env and start Worker before engines launch."""
        if not self._kv_cfg.enable or self._kv_cfg.backend != "yuanrong":
            return
        self._export_client_env()

        self.pull()
        # Wait briefly so engines can connect on first use
        deadline = time.monotonic() + WORKER_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._worker_ready():
                logger.info(
                    "Yuanrong Worker is ready at %s:%s",
                    self._host_ip,
                    self._worker_port,
                )
                return
            time.sleep(0.5)
        logger.warning(
            "Yuanrong Worker is not ready yet at %s after prepare; engines may retry",
            self._worker_address,
        )

    def pull(self) -> None:
        """Start Yuanrong Worker if not already running (idempotent)."""
        if not self._kv_cfg.enable or self._kv_cfg.backend != "yuanrong":
            return
        with self._lock:
            self._pull_locked()

    def _pull_locked(self) -> None:
        """Idempotent Worker start — caller must hold ``self._lock``.

        Factored out so ``health_check()`` can restart the Worker under the
        same lock without deadlocking on the non-reentrant ``threading.Lock``.
        """
        if (self._proc is not None and self._proc.poll() is None) or self._worker_process_exists():
            self._started = True
            if self._ready_deadline is None:
                self._ready_deadline = time.monotonic() + WORKER_READY_TIMEOUT_S
            return
        self._export_client_env()
        cmd = self._build_cmd()
        logger.info("Starting Yuanrong Worker: %s", shlex.join(cmd))
        try:
            self._proc = subprocess.Popen(  # pylint: disable=consider-using-with
                cmd,
                start_new_session=True,
            )
            self._started = True
            self._ready_deadline = time.monotonic() + WORKER_READY_TIMEOUT_S
            returncode = self._proc.poll()
            if returncode is not None and returncode != 0:
                raise RuntimeError(f"Yuanrong Worker exited with code {returncode}")
        except (OSError, RuntimeError):
            self._started = False
            self._ready_deadline = None
            self._proc = None
            raise
        if returncode is not None and not self._worker_ready():
            logger.info(
                "Yuanrong Worker CLI daemonized; Worker is not ready yet at %s",
                self._worker_address,
            )
        elif returncode is not None:
            logger.info(
                "Yuanrong Worker started (daemonized, addr=%s)",
                self._worker_address,
            )
        else:
            logger.info(
                "Yuanrong Worker started (pid=%s, addr=%s)",
                self._proc.pid,
                self._worker_address,
            )

    def stop(self) -> None:
        with self._lock:
            self._stop_worker_locked()
            self._started = False
            self._ready_deadline = None

    def _stop_worker_locked(self) -> None:
        """Stop the launcher and daemonized Worker while holding ``self._lock``."""
        should_stop_worker = self._started or self._proc is not None or self._worker_process_exists()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
                except OSError:
                    logger.exception("Failed to kill Yuanrong Worker")
                except subprocess.TimeoutExpired:
                    logger.warning("Timed out waiting for Yuanrong Worker launcher to exit after kill")
        if should_stop_worker:
            cmd = ["dscli", "stop", "--worker_address", self._worker_address]
            logger.info("Stopping Yuanrong Worker: %s", shlex.join(cmd))
            try:
                result = subprocess.run(cmd, check=False, timeout=WORKER_STOP_TIMEOUT_S)
                if result.returncode != 0:
                    logger.warning(
                        "Yuanrong Worker stop command failed at %s (returncode=%s); checking for forced cleanup",
                        self._worker_address,
                        result.returncode,
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Yuanrong Worker stop command failed at %s: %s", self._worker_address, exc)
            if self._worker_process_exists():
                self._kill_worker_processes()
                if self._worker_process_exists():
                    raise RuntimeError("Yuanrong Worker is still running after forced cleanup")
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("Yuanrong Worker is still running after stop")
        self._proc = None

    def health_check(self) -> None:
        """Restart Worker when the managed process / port is down.

        ``_started`` stays True across failed restarts so the next monitor tick
        keeps retrying — a failed ``pull()`` must not permanently disarm the
        health check (upstream review feedback).  A capped exponential backoff
        (10s * 2^(n-1), max 5 min) prevents a hot restart loop when the Worker
        cannot come up.

        The entire state machine runs under ``self._lock`` to prevent a race
        with ``pull()`` on the API thread: without the lock, the monitor could
        observe a dead Worker, then ``pull()`` on the API thread spawns a fresh
        one, then the monitor overwrites ``self._proc`` with ``None``, dropping
        the live ``Popen`` handle and causing a duplicate Worker launch on the
        next tick.  The inner call to ``_pull_locked()`` reuses the same lock
        instead of acquiring it again (the lock is non-reentrant).
        """
        if not self._started:
            return
        with self._lock:
            if not self._started:
                return
            phase = "inspect"
            try:
                if self._worker_ready():
                    self._ready_deadline = None
                    self._restart_failures = 0
                    return
                launcher_alive = self._proc is not None and self._proc.poll() is None
                worker_exists = self._worker_process_exists()
                if launcher_alive or worker_exists:
                    if self._ready_deadline is None:
                        self._ready_deadline = time.monotonic() + WORKER_READY_TIMEOUT_S
                    if time.monotonic() < self._ready_deadline:
                        return
                if self._restart_failures and time.monotonic() < self._next_restart_ts:
                    return  # in backoff window after a previous failed restart
                logger.warning(
                    "Yuanrong Worker is not ready (restart_worker=%s)",
                    self.restart_worker,
                )
                if not self.restart_worker:
                    self._started = False
                    return
                if launcher_alive or worker_exists:
                    phase = "stop"
                    self._stop_worker_locked()
                else:
                    self._proc = None
                self._ready_deadline = None
                phase = "start"
                self._pull_locked()
            except Exception:
                self._restart_failures += 1
                delay = min(
                    RESTART_BACKOFF_BASE_S * (2 ** min(self._restart_failures - 1, 5)),
                    RESTART_BACKOFF_MAX_S,
                )
                self._next_restart_ts = time.monotonic() + delay
                # _pull_locked() clears _started on failure; restore it so the
                # next tick retries once the backoff window elapses.
                self._started = True
                logger.exception(
                    "Failed to %s Yuanrong Worker during recovery (attempt %d, next retry in %.0fs)",
                    phase,
                    self._restart_failures,
                    delay,
                )
