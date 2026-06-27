# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading
import time

from motor.common.http.http_client import SafeHTTPSClient
from motor.common.logger import get_logger
from motor.config.endpoint import EndpointConfig
from motor.engine_server.core.snapshot_monitor import SnapshotMonitor
from motor.common.utils.snapshot_utils import (
    is_restored_from_host_side_snapshot,
    load_snapshot_metadata,
    get_pod_ip,
    RETRY_LOG_FREQUENCY,
)
from motor.engine_server.utils.ip import build_endpoint

logger = get_logger(__name__)

SUSPEND_TIMEOUT = 3600.0
RESUME_TIMEOUT = 3600.0
DEVICE_UNLOCK_TIMEOUT = 10

RETRY_INTERVAL = 1.0


class SnapshotSentinel(threading.Thread):
    def __init__(self, endpoint_config: EndpointConfig, name: str = "snapshot-sentinel", daemon: bool = True):
        super().__init__(name=name, daemon=daemon)
        self._snapshot_metadata = endpoint_config.snapshot_metadata
        self._infer_port = endpoint_config.port
        self._infer_tls = endpoint_config.deploy_config.infer_tls_config
        self._health_timeout = float(endpoint_config.deploy_config.health_check_config.health_collector_timeout)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self._wait_until_infer_healthy()
        if self._stop_event.is_set():
            logger.info("[snapshot] Snapshot sentinel stopped before infer became healthy")
            return
        logger.info("[snapshot] Infer is healthy, starting to suspend")

        # pre-snapshot
        self._call_suspend()
        if self._stop_event.is_set():
            logger.info("[snapshot] Snapshot sentinel stopped before suspend completed")
            return

        # reach checkpoint
        self._reach_checkpoint()
        if self._stop_event.is_set():
            logger.info("[snapshot] Snapshot sentinel stopped, maybe current is cold start and reach checkpoint")
            return

        # post-snapshot
        logger.info("[snapshot] Restored from host-side snapshot, starting to resume")
        self._call_resume()

    def _wait_until_infer_healthy(self) -> None:
        retries = 0
        infer_address = build_endpoint(get_pod_ip(), self._infer_port)
        while not self._stop_event.is_set():
            try:
                with SafeHTTPSClient(
                    address=infer_address,
                    tls_config=self._infer_tls,
                    timeout=self._health_timeout,
                ) as client:
                    resp = client.do_get("health")
                    if resp.content.decode("utf-8").lower() == "true":
                        return
                    retries += 1
            except Exception as e:
                if retries % RETRY_LOG_FREQUENCY == 0:
                    logger.warning("[snapshot] Infer health check failed, will retry: %s", str(e))
                retries += 1
            time.sleep(RETRY_INTERVAL)

    def _reach_checkpoint(self) -> None:
        retries = 0
        while not self._stop_event.is_set() and not is_restored_from_host_side_snapshot():
            # If current is restored from snapshot and directly reach checkpoint, break idle loop to resume
            try:
                checkpoint = load_snapshot_metadata(self._snapshot_metadata, "checkpoint")
                if checkpoint != "done" and not is_restored_from_host_side_snapshot():
                    # Current is cold start and do not reach checkpoint
                    raise ValueError("Current is cold start and checkpoint is not done")
                elif checkpoint == "done" and not is_restored_from_host_side_snapshot():
                    # Current is cold start and reach checkpoint, unlock device and stop snapshot sentinel
                    infer_address = build_endpoint(get_pod_ip(), self._infer_port)
                    with SafeHTTPSClient(
                        address=infer_address,
                        tls_config=self._infer_tls,
                        timeout=DEVICE_UNLOCK_TIMEOUT,
                    ) as client:
                        client.do_post(
                            "device_unlock",
                        )
                    logger.info(
                        "[snapshot] Reach checkpoint, should unlock device and stop snapshot sentinel during cold start"
                    )
                    SnapshotMonitor().mark_unlock_done()
                    self._stop_event.set()
                    return
            except Exception as e:
                if retries % RETRY_LOG_FREQUENCY == 0:
                    logger.warning("[snapshot] Do not reach Checkpoint, will retry inspect: %s", str(e))
                retries += 1

            time.sleep(RETRY_INTERVAL)

    def _call_suspend(self) -> None:
        retries = 0
        while not self._stop_event.is_set():
            model_save_path = None
            try:
                model_save_path = load_snapshot_metadata(self._snapshot_metadata, "model_save_path")
                infer_address = build_endpoint(get_pod_ip(), self._infer_port)
                with SafeHTTPSClient(
                    address=infer_address,
                    tls_config=self._infer_tls,
                    timeout=SUSPEND_TIMEOUT,
                ) as client:
                    client.do_post(
                        "suspend",
                        query_params={"model_save_path": model_save_path},
                    )
                logger.info(
                    "[snapshot] Suspend completed, model_save_path=%r",
                    model_save_path,
                )
                SnapshotMonitor().mark_suspend_done()
                return
            except Exception as e:
                if retries % RETRY_LOG_FREQUENCY == 0:
                    logger.warning(
                        "[snapshot] Suspend request failed %s times, will retry, model_save_path=%r: %s",
                        retries,
                        model_save_path,
                        str(e),
                    )
                retries += 1
                time.sleep(RETRY_INTERVAL)

        logger.error("[snapshot] Suspend request failed after maximum retries.")

    def _call_resume(self) -> None:
        retries = 0
        while not self._stop_event.is_set():
            model_load_path = None
            data_parallel_master_ip = None
            try:
                # fetch snapshot metadata(model_load_path, data_parallel_master_ip) from snapshot metadata file
                # if necessary metadata is not provided, raise ValueError and retry resume
                model_load_path = load_snapshot_metadata(self._snapshot_metadata, "model_load_path")
                data_parallel_master_ip = load_snapshot_metadata(self._snapshot_metadata, "data_parallel_master_ip")
                infer_address = build_endpoint(get_pod_ip(), self._infer_port)
                with SafeHTTPSClient(
                    address=infer_address,
                    tls_config=self._infer_tls,
                    timeout=RESUME_TIMEOUT,
                ) as client:
                    client.do_post(
                        "resume",
                        query_params={
                            "model_path": model_load_path,
                            "data_parallel_master_ip": data_parallel_master_ip,
                        },
                    )
                logger.info(
                    "[snapshot] Resume completed, model_path=%r, data_parallel_master_ip=%r",
                    model_load_path,
                    data_parallel_master_ip,
                )
                SnapshotMonitor().mark_resume_done()
                return
            except Exception as e:
                if retries % RETRY_LOG_FREQUENCY == 0:
                    logger.warning(
                        "[snapshot] Resume request failed %s times, will retry, model_path=%r, data_parallel_master_ip=%r: %s",
                        retries,
                        model_load_path,
                        data_parallel_master_ip,
                        str(e),
                    )
                retries += 1
                time.sleep(RETRY_INTERVAL)

        logger.error("[snapshot] Resume request failed after maximum retries.")
