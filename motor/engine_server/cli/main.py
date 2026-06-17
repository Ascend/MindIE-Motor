# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import sys

from motor.common.utils.process_utils import set_process_title


def _dp_rank_from_argv() -> int:
    argv = sys.argv[1:]
    for idx, arg in enumerate(argv):
        if arg == "--dp-rank" and idx + 1 < len(argv):
            try:
                return int(argv[idx + 1])
            except ValueError:
                return 0
        if arg.startswith("--dp-rank="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                return 0
    return 0


set_process_title(f"EngineServer-DP{_dp_rank_from_argv()}")

# ruff: noqa: E402
from motor.common.logger import get_logger
from motor.config.endpoint import EndpointConfig
from motor.engine_server.factory.config_factory import ConfigFactory
from motor.engine_server.factory.endpoint_factory import EndpointFactory
from motor.engine_server.utils.prometheus import setup_multiprocess_prometheus

logger = get_logger(__name__)


def main():
    # Execute setup_multiprocess_prometheus before importing Endpoint to ensure
    # PROMETHEUS_MULTIPROC_DIR is detected when Prometheus low-level code creates ValueClass.
    setup_multiprocess_prometheus()

    from motor.engine_server.core.infer_endpoint import InferEndpoint
    from motor.engine_server.core.mgmt_endpoint import MgmtEndpoint

    endpoint_config = EndpointConfig.init_endpoint_config()
    config_factory = ConfigFactory(endpoint_config=endpoint_config)
    config = config_factory.parse()
    logger.info("successfully parsed %s engine configuration", endpoint_config.engine_type)

    snapshot_sentinel = None
    if endpoint_config.snapshot_metadata is not None:
        from motor.engine_server.core.snapshot_sentinel import SnapshotSentinel

        snapshot_sentinel = SnapshotSentinel(endpoint_config)
        snapshot_sentinel.start()
        logger.info(
            "[snapshot] Snapshot metadata given, launching snapshot sentinel thread "
            "to save the device-side snapshot once the inference service is ready."
        )

    mgmt_endpoint: MgmtEndpoint = MgmtEndpoint(config)
    infer_endpoint: InferEndpoint = EndpointFactory().get_infer_endpoint(config)

    mgmt_endpoint.run()
    infer_endpoint.run()
    infer_endpoint.wait()

    logger.info("shutting down endpoints and child processes...")
    mgmt_endpoint.shutdown()
    infer_endpoint.shutdown()
    if snapshot_sentinel is not None:
        snapshot_sentinel.stop()
        snapshot_sentinel.join(timeout=5)
        if snapshot_sentinel.is_alive():
            logger.warning("[snapshot] snapshot sentinel thread did not exit within timeout")
    logger.info("endpoints and child processes shut down")


if __name__ == "__main__":
    main()
