# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import os
import socket
from multiprocessing import connection
from multiprocessing.process import BaseProcess
from typing import Any

import uvicorn

try:
    import uvloop
except ImportError:
    uvloop = None

from motor.common.http.cert_util import CertUtil
from motor.common.utils.config_watcher import ConfigWatcher
from motor.common.http.http_client import HTTPClientPool
from motor.common.utils.net import detect_family, format_address
from motor.common.logger import get_logger, reconfigure_logging
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_server.inference_server import InferenceServer
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.process.base import BaseProcessManager
from motor.coordinator.process.utils import set_process_title
from motor.coordinator.scheduler.policy.kv_cache_affinity import TokenizerManager

logger = get_logger(__name__)


def _socket_host(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def run_inference_worker_proc(
    listen_address: tuple[str, int],
    sock: socket.socket,
    config: CoordinatorConfig,
    worker_index: int,
    **uvicorn_kwargs: Any,
) -> None:
    """Entrypoint for individual Inference worker processes.

    Args:
        listen_address: Address to listen for client connections
        sock: Socket for client connections (shared between processes)
        config: Coordinator configuration
        worker_index: Index of this worker process
        **uvicorn_kwargs: Additional uvicorn configuration
    """
    inference_server = None  # Set before use so finally can safely disconnect

    # Reconfigure logging so this child process writes to the same log_file as daemon and Scheduler processes
    reconfigure_logging(config.logging_config)

    # Set process title
    set_process_title(name=str(worker_index))

    logger.info(f"Inference worker process {worker_index} starting (PID: {os.getpid()})")

    # Create RequestManager first, then InferenceServer (business plane only)
    request_manager = RequestManager(config)
    inference_server = InferenceServer(config, request_manager=request_manager)
    inference_server.setup_rate_limiting(config.rate_limit_config)

    worker_config_watcher = None

    # In multi-process: Worker watches config file so hot-reload reaches this process
    if config.config_path and os.path.exists(config.config_path):
        try:

            def _worker_config_updated() -> None:
                inference_server.update_config(config)
                request_manager.update_config(config)

            worker_config_watcher = ConfigWatcher(
                config_path=config.config_path,
                reload_callback=config.reload,
                config_update_callback=_worker_config_updated,
            )
            worker_config_watcher.start()
            logger.info(
                "Worker %s: config watcher started for hot-reload: %s",
                worker_index,
                config.config_path,
            )
        except Exception as e:
            logger.warning(
                "Worker %s: failed to start config watcher (hot-reload disabled): %s",
                worker_index,
                e,
            )

    # init TokenizerManager
    TokenizerManager(config)

    # Get the inference app and configure uvicorn
    app = inference_server.app
    config_kwargs = InferenceServer.create_base_uvicorn_config(
        app,
        config.api_config.coordinator_api_host,
        config.api_config.coordinator_api_infer_port,
    )
    inference_server.apply_timeout_to_config(config_kwargs)

    # Create uvicorn config
    uvicorn_config = uvicorn.Config(**config_kwargs)
    uvicorn_config.load()

    # Add SSL configuration if needed (must be set after Config creation)
    if config.infer_tls_config.enable_tls:
        ssl_context = CertUtil.create_ssl_context(tls_config=config.infer_tls_config)
        if ssl_context:
            uvicorn_config.ssl = ssl_context

    # Create and run server(s)
    server = uvicorn.Server(uvicorn_config)

    async def _run_servers():
        await server.serve(sockets=[sock] if sock else None)

    try:
        # Run server with shared socket.
        # Note: Multiple processes can share the same socket with SO_REUSEPORT
        # The OS kernel will distribute connections among processes
        # Each process will handle requests independently with its own engine clients
        if uvloop is not None:
            uvloop.run(_run_servers())
        else:
            asyncio.run(_run_servers())
    except KeyboardInterrupt:
        logger.info(f"Inference worker process {worker_index} received interrupt signal")
    except Exception as e:
        logger.error(f"Inference worker process {worker_index} error: {e}", exc_info=True)
        raise
    finally:
        # Stop config watcher if started
        if worker_config_watcher is not None:
            try:
                worker_config_watcher.stop()
            except Exception as e:
                logger.warning(
                    "Ignored error stopping config watcher in worker %s: %s",
                    worker_index,
                    e,
                )
        # Disconnect SchedulerClient so ZMQ connections are closed cleanly
        if inference_server is not None:
            conn = getattr(inference_server, "_scheduler_connection", None)
            if conn is not None:
                try:
                    asyncio.run(conn.disconnect())
                except Exception as e:
                    logger.warning(
                        "Ignored error disconnecting scheduler client in worker %s: %s",
                        worker_index,
                        e,
                    )

        # Close HTTP client pool connections
        try:
            client_pool = HTTPClientPool()
            try:
                asyncio.run(client_pool.close_all())
                logger.info(f"HTTP client pool closed in worker process {worker_index}")
            except Exception as loop_error:
                logger.warning(
                    "Failed to close HTTP client pool in worker %s: %s",
                    worker_index,
                    loop_error,
                    exc_info=True,
                )
        except Exception as e:
            logger.warning(
                f"Failed to close HTTP client pool in worker {worker_index}: {e}",
                exc_info=True,
            )

        if sock:
            try:
                sock.close()
            except Exception as e:
                logger.warning("Ignored error closing socket in worker %s: %s", worker_index, e)
        logger.info(f"Inference worker process {worker_index} stopped")


class InferenceProcessManager(BaseProcessManager):
    """
    Manages a group of Inference API server worker processes.

    Similar to vllm's APIServerProcessManager, handles creation,
    monitoring, and termination of API server worker processes.
    Uses start()/stop() so it can be registered in main.processes and
    started/stopped by start_all_processes()/stop_all_processes().
    """

    def __init__(
        self,
        config: CoordinatorConfig,
        listen_address: tuple[str, int],
        sock: socket.socket,
        num_workers: int,
    ):
        super().__init__(config, process_name="InferenceWorkers")
        self.listen_address = listen_address
        self.sock = sock
        self.num_workers = num_workers

    def wait_for_completion(self) -> None:
        """Wait for all processes to complete or detect if any fail"""
        try:
            logger.info("Waiting for Inference API server processes to complete...")
            sentinel_to_proc: dict[Any, BaseProcess] = {proc.sentinel: proc for proc in self._processes}
            # Wait for any process to terminate (loop until all sentinels are consumed)
            while sentinel_to_proc:
                # Wait for any process to terminate
                ready_sentinels: list[Any] = connection.wait(sentinel_to_proc, timeout=5)

                # Process any terminated processes
                for sentinel in ready_sentinels:
                    proc = sentinel_to_proc.pop(sentinel)
                    # Check if process exited with error
                    if proc.exitcode != 0:
                        raise RuntimeError(f"Process {proc.name} (PID: {proc.pid}) died with exit code {proc.exitcode}")
                    else:
                        logger.info(f"Process {proc.name} (PID: {proc.pid}) exited normally")

        except KeyboardInterrupt:
            logger.info("Received KeyboardInterrupt, shutting down Inference API servers...")
        except Exception as e:
            logger.exception("Exception occurred while running Inference API servers: %s", e)
            raise
        finally:
            logger.info("Terminating remaining processes...")
            self.stop()

    def close(self) -> None:
        """Alias for stop(); kept for backward compatibility."""
        self.stop()

    def is_running(self) -> bool:
        """True only if all worker processes are alive. Any single exit triggers restart."""
        if not self._processes:
            return False
        return all(p.is_alive() for p in self._processes)

    def restart_dead_workers(self) -> bool:
        """Replace and start only dead worker process(es). Leaves alive workers running."""
        dead_indices = [i for i, p in enumerate(self._processes) if not p.is_alive()]
        if not dead_indices:
            return True
        logger.warning(
            "Restarting %s dead worker(s) at index(es) %s",
            self.process_name,
            dead_indices,
        )
        for i in dead_indices:
            try:
                proc = self._create_process(i)
                proc.start()
                self._processes[i] = proc
                logger.info(
                    "Started %s process %s (PID: %s) replacing dead worker",
                    self.process_name,
                    i,
                    proc.pid,
                )
            except Exception as e:
                logger.error(
                    "Failed to restart %s worker %s: %s",
                    self.process_name,
                    i,
                    e,
                    exc_info=True,
                )
        return self.is_running()

    def _create_process(self, index: int) -> BaseProcess:
        return self._spawn_context.Process(
            target=run_inference_worker_proc,
            name=f"InferenceWorker-{index}",
            args=(self.listen_address, self.sock, self.config, index),
        )

    def _get_process_count(self) -> int:
        return self.num_workers


def create_shared_socket(host: str, port: int) -> socket.socket | None:
    """Create a socket that can be shared between multiple processes

    Args:
        host: Host address
        port: Port number

    Returns:
        Socket that can be shared between processes, or None if not supported
    """
    bind_host = _socket_host(host)
    sock = socket.socket(detect_family(bind_host), socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # SO_REUSEPORT allows multiple processes to bind to the same port (coordinator is Linux-only).
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        logger.warning("SO_REUSEPORT not available on this platform")
        sock.close()
        return None

    try:
        sock.bind((bind_host, port))
        sock.listen(128)  # Backlog
        logger.info(f"Created shared socket on {format_address(host, port)}")
        return sock
    except Exception as e:
        logger.error(f"Failed to bind socket on {format_address(host, port)}: {e}")
        sock.close()
        return None
