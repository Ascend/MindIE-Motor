# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Prestop graceful shutdown for Kubernetes PreStop hook.

Flow:
1. Read NodeManager port from user_config.json (api_config.node_manager_port)
2. POST /node-manager/pause to local NodeManager → endpoints set to PAUSED
3. Read engine_mgmt_addrs from the pause response
4. Poll engine /metrics locally (num_requests_waiting / running) until
   all drain to zero, then exit (Pod terminates).

Config is read from CONFIG_PATH or CONFIGMAP_PATH env var (same pattern as probe.py).
"""

import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Default values
DEFAULT_NM_PORT = 1026
DEFAULT_MAX_WAIT_SECONDS = 15
DEFAULT_POLL_INTERVAL = 3
HTTP_TIMEOUT = 10.0


def format_address(host, port):
    try:
        if isinstance(ipaddress.ip_address(host.strip("[]")), ipaddress.IPv6Address):
            return f"[{host.strip('[]')}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def get_val_by_key_path(config, key_path):
    keys = key_path.split('.')
    config_element = config
    for key in keys:
        if not isinstance(config_element, dict) or key not in config_element:
            return None
        config_element = config_element[key]
    return config_element


CONFIG_SEARCH_PATHS = [
    os.environ.get("CONFIGMAP_PATH", ""),  # /mnt/configmap
    os.environ.get("CONFIG_PATH", ""),  # /usr/local/Ascend/pyMotor/conf
]


def load_config():
    """Load user_config.json — tries configmap mount first, then CONFIG_PATH."""
    for base_path in CONFIG_SEARCH_PATHS:
        if not base_path:
            continue
        user_config_path = base_path
        if os.path.isdir(user_config_path):
            user_config_path = os.path.join(user_config_path, "user_config.json")
        if os.path.exists(user_config_path):
            try:
                with open(user_config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if isinstance(config, dict):
                    logger.info("Loaded config from: %s", user_config_path)
                    return config
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", user_config_path, e)
                continue

    logger.error("user_config.json not found in any search path")
    return None


NM_PORT_PATHS = [
    "motor_nodemanger_config.api_config.node_manager_port",
    "motor_engine_prefill_config.motor_nodemanger_config.api_config.node_manager_port",
    "motor_engine_decode_config.motor_nodemanger_config.api_config.node_manager_port",
]


def get_nm_port(config):
    """Read NodeManager port from config JSON, with default fallback."""
    for path in NM_PORT_PATHS:
        port = get_val_by_key_path(config, path)
        if isinstance(port, int) and 1024 <= port <= 65535:
            return port
    logger.info("node_manager_port not in config, using default %d", DEFAULT_NM_PORT)
    return DEFAULT_NM_PORT


def _http_post_json(url, timeout=10):
    """HTTP POST via curl. Returns (status_code, body_text) or (None, None)."""
    try:
        result = subprocess.run(  # nosec B607
            [
                "curl",
                "-s",
                "-w",
                "\n%{http_code}",
                "-X",
                "POST",
                url,
                "-H",
                "Content-Type: application/json",
                "-d",
                "{}",
                "--connect-timeout",
                str(timeout),
                "--max-time",
                str(timeout),
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
        output = result.stdout.strip()
        if not output:
            return None, None
        lines = output.rsplit("\n", 1)
        if len(lines) == 2:
            return int(lines[1]), lines[0]
        return None, output
    except Exception as e:
        logger.error("curl POST failed: %s", e)
        return None, None


def _http_get_text(url, timeout=5):
    """HTTP GET via curl. Returns response body text or None."""
    try:
        result = subprocess.run(  # nosec B607
            ["curl", "-s", "-X", "GET", url, "--connect-timeout", str(timeout), "--max-time", str(timeout)],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        logger.debug("curl GET failed: returncode=%s, stderr=%s", result.returncode, result.stderr[:200])
    except Exception as e:
        logger.debug("curl GET failed: %s", e)
    return None


def send_pause(node_manager_url, config):
    """POST /node-manager/pause to local NodeManager.

    Returns parsed JSON response dict, or None on failure.
    """
    url = f"{node_manager_url}/node-manager/pause"
    status, body = _http_post_json(url, timeout=HTTP_TIMEOUT)
    if status == 200 and body:
        try:
            result = json.loads(body)
            logger.info("Pause response: %s", result)
            return result
        except json.JSONDecodeError as e:
            logger.error("Failed to parse pause response: %s", e)
    else:
        logger.error("Pause failed: status=%s, body=%s", status, body)
    return None


def get_engine_metrics(engine_mgmt_addr):
    """Query engine /metrics locally and sum num_requests_waiting/running."""
    url = f"http://{engine_mgmt_addr}/metrics"
    text = _http_get_text(url, timeout=10)
    if not text:
        return None

    waiting = 0
    running = 0
    for line in text.split("\n"):
        if line.startswith("#") or not line.strip():
            continue
        m = re.search(r'\b(\d+(?:\.\d+)?)\s*$', line)
        if not m:
            continue
        val = int(float(m.group(1)))
        if "num_requests_waiting" in line:
            waiting += val
        elif "num_requests_running" in line:
            running += val

    return {"waiting": waiting, "running": running}


def main():
    """Main prestop function.

    Usage: python prestop.py [--max-wait SECONDS] [--poll-interval SECONDS]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Prestop graceful shutdown")
    parser.add_argument(
        "--max-wait",
        type=int,
        default=DEFAULT_MAX_WAIT_SECONDS,
        help=f"Max wait seconds (default: {DEFAULT_MAX_WAIT_SECONDS})",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Poll interval seconds (default: {DEFAULT_POLL_INTERVAL})",
    )
    args = parser.parse_args()

    # Load config (same pattern as probe.py)
    config = load_config()
    if config is None:
        logger.error("Failed to load config, exiting")
        sys.exit(0)

    nm_port = get_nm_port(config)
    pod_ip = os.environ.get("POD_IP", "127.0.0.1")
    nm_url = f"http://{format_address(pod_ip, nm_port)}"
    logger.info("NodeManager URL: %s", nm_url)

    # Step 1: Send pause to NodeManager
    logger.info("Sending pause to NodeManager at %s", nm_url)
    response = send_pause(nm_url, config)
    if response is None:
        logger.warning("Terminate request failed, exiting anyway")
        sys.exit(0)

    engine_mgmt_addrs = response.get("engine_mgmt_addrs", [])
    if not engine_mgmt_addrs:
        logger.error("No engine_mgmt_addrs in pause response, exiting")
        sys.exit(0)

    logger.info("Engine mgmt addresses: %s", engine_mgmt_addrs)

    # Step 2: Poll engine /metrics locally until requests drain
    logger.info("Polling engine metrics...")
    start_time = time.time()

    while time.time() - start_time < args.max_wait:
        total_waiting = 0
        total_running = 0
        all_ok = True

        for addr in engine_mgmt_addrs:
            metrics = get_engine_metrics(addr)
            if metrics is None:
                logger.debug("Engine %s unreachable", addr)
                all_ok = False
                break
            total_waiting += metrics["waiting"]
            total_running += metrics["running"]

        if not all_ok:
            logger.warning("Engine unreachable, exiting")
            break

        total_active = total_waiting + total_running
        logger.info("active=%d (waiting=%d, running=%d)", total_active, total_waiting, total_running)

        if total_active == 0:
            elapsed = time.time() - start_time
            logger.info("All requests drained after %.1fs", elapsed)
            break

        time.sleep(args.poll_interval)
    else:
        elapsed = time.time() - start_time
        logger.info("Timeout after %.1fs, stopping anyway", elapsed)

    sys.exit(0)


if __name__ == "__main__":
    main()
