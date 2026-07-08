# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import shlex
import signal
import subprocess
import sys

from motor.common.utils.process_utils import set_process_title


# Set this to ``True`` to enable native CLI launch mode.
# When enabled, the engine (vLLM / SGLang) is launched
# via its native CLI command (e.g. ``vllm serve ...``) in a subprocess
# instead of the default invasive in-process launch.
NATIVE_LAUNCH_ENABLED = False


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


def _log_safe_cmd(cmd: list[str]) -> str:
    """Format a command list for logging, escaping arguments that contain spaces."""
    return " ".join(shlex.quote(a) if " " in a else a for a in cmd)


def _build_native_launch_cmd(config) -> list[str]:
    """Build the native CLI command for the engine from the parsed config.

    Returns a list suitable for ``subprocess.Popen``, e.g.
    ``["vllm", "serve", "--model", "...", "--host", "0.0.0.0", ...]``.

    Security:
        * ``engine_type`` is validated against a strict whitelist.
        * ``subprocess.Popen`` is called with a list (no ``shell=True``) so
          argument values (including JSON) are passed as literals.
    """
    engine_type = config.get_endpoint_config().engine_type
    cli_args = config.get_cli_args()

    if engine_type == "vllm":
        return ["vllm", "serve"] + cli_args
    elif engine_type == "sglang":
        return ["python3", "-m", "sglang.launch_server"] + cli_args
    else:
        raise ValueError(
            f"Unsupported engine type for native launch: {engine_type}. Supported types are: vllm, sglang."
        )


def _run_native(config) -> None:
    """Launch the engine via native CLI command in a subprocess."""
    cmd = _build_native_launch_cmd(config)
    logger.info("Launching engine via native command: %s", _log_safe_cmd(cmd))

    with subprocess.Popen(cmd) as process:

        def _signal_handler(signum, frame):
            logger.info("Received signal %s, forwarding SIGTERM to native engine process", signum)
            process.send_signal(signal.SIGTERM)

        old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
        old_sigint = signal.signal(signal.SIGINT, _signal_handler)

        try:
            process.wait()
        finally:
            signal.signal(signal.SIGTERM, old_sigterm)
            signal.signal(signal.SIGINT, old_sigint)

    logger.info("Native engine process exited with code %s", process.returncode)


def main():
    # Execute setup_multiprocess_prometheus before importing Endpoint to ensure
    # PROMETHEUS_MULTIPROC_DIR is detected when Prometheus low-level code creates ValueClass.
    setup_multiprocess_prometheus()

    from motor.engine_server.core.infer_endpoint import InferEndpoint
    from motor.engine_server.core.mgmt_endpoint import MgmtEndpoint

    endpoint_config = EndpointConfig.init_endpoint_config()

    mgmt_endpoint: MgmtEndpoint = MgmtEndpoint(endpoint_config)
    mgmt_endpoint.run()

    config_factory = ConfigFactory(endpoint_config=endpoint_config)
    config = config_factory.parse()
    logger.info("successfully parsed %s engine configuration", endpoint_config.engine_type)

    mgmt_endpoint.attach_engine(config)

    if NATIVE_LAUNCH_ENABLED:
        logger.info(
            "Native launch mode enabled (%s), launching engine via CLI subprocess.",
            NATIVE_LAUNCH_ENABLED,
        )
        if endpoint_config.snapshot_metadata is not None:
            logger.warning(
                "Snapshot metadata is provided, but native launch mode is enabled. "
                "Snapshot sentinel will not be started, and snapshot saving may not work as expected."
            )
        try:
            _run_native(config)
        finally:
            mgmt_endpoint.shutdown()
        return

    snapshot_sentinel = None
    if endpoint_config.snapshot_metadata is not None:
        from motor.engine_server.core.snapshot_sentinel import SnapshotSentinel

        snapshot_sentinel = SnapshotSentinel(endpoint_config)
        snapshot_sentinel.start()
        logger.info(
            "[snapshot] Snapshot metadata given, launching snapshot sentinel thread "
            "to save the device-side snapshot once the inference service is ready."
        )

    infer_endpoint: InferEndpoint = EndpointFactory().get_infer_endpoint(config)
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
