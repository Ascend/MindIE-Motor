# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import ipaddress
import json
import logging
import os
import sys
from enum import Enum

import httpx

MOTOR_DEPLOY_CONFIG = "motor_deploy_config"
TLS_CONFIG = "tls_config"
MGMT_TLS_CONFIG = "mgmt_tls_config"
ENABLE_TLS = "enable_tls"
CA_FILE = "ca_file"
CERT_FILE = "cert_file"
KEY_FILE = "key_file"


class ConfigKey(Enum):
    MOTOR_CONTROLLER = "motor_controller_config"
    MOTOR_COORDINATOR = "motor_coordinator_config"
    MOTOR_KV_STORE = "kv_cache_store_config"
    MOTOR_NODEMANAGER_UNION = "motor_engine_union_config.motor_nodemanger_config"
    MOTOR_NODEMANAGER_PREFILL = "motor_engine_prefill_config.motor_nodemanger_config"
    MOTOR_NODEMANAGER_DECODE = "motor_engine_decode_config.motor_nodemanger_config"


# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Hard-coded URLs for all probe types
PROBE_URLS = {'startup': '/startup', 'readiness': '/readiness', 'liveness': '/liveness'}

# Hard-coded default ports
DEFAULT_PORTS = {
    'controller': 1026,
    'coordinator': 1026,
    'node_manager': 1026,
}


ENGINE_ROLES = ("union", "prefill", "decode")

ROLE_CONFIG_PATHS = {
    "controller": ConfigKey.MOTOR_CONTROLLER.value,
    "coordinator": ConfigKey.MOTOR_COORDINATOR.value,
    "union": ConfigKey.MOTOR_NODEMANAGER_UNION.value,
    "prefill": ConfigKey.MOTOR_NODEMANAGER_PREFILL.value,
    "decode": ConfigKey.MOTOR_NODEMANAGER_DECODE.value,
}

# HTTP request timeout
TIMEOUT = 600


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
            logger.info("Key '%s' not found in config: %s", key, key_path)
            return None
        config_element = config_element[key]
    return config_element


def get_builtin_default_port(role):
    """
    Get built-in default port when JSON config is not available.

    Args:
        role: 'controller' or 'coordinator'

    Returns:
        Built-in default port number, or -1 if not found
    """
    port = DEFAULT_PORTS.get(role)
    if port is not None:
        logger.info("Using hard-coded default port: %s", port)
        return port
    else:
        logger.error("Unknown role: %s", role)
        return -1


def _get_mgmt_tls_config(user_config):
    if not isinstance(user_config, dict):
        return None
    deploy_config = user_config.get(MOTOR_DEPLOY_CONFIG)
    if not isinstance(deploy_config, dict):
        return None
    tls_config = deploy_config.get(TLS_CONFIG)
    if not isinstance(tls_config, dict):
        return None
    mgmt_tls_config = tls_config.get(MGMT_TLS_CONFIG)
    if not isinstance(mgmt_tls_config, dict):
        return None
    return mgmt_tls_config


def get_config(role):
    config_path = os.environ.get('CONFIG_PATH')
    if not config_path:
        logger.error("CONFIG_PATH environment variable not set")
        return -1

    user_config_path = config_path
    if os.path.isdir(user_config_path):
        user_config_path = os.path.join(user_config_path, 'user_config.json')

    if not os.path.exists(user_config_path):
        logger.error("User config file does not exist: %s", user_config_path)
        return -1

    try:
        with open(user_config_path, 'r', encoding='utf-8') as file:
            user_config = json.load(file)
    except Exception as e:
        logger.error("Failed to load JSON config %s: %s", user_config_path, e)
        return -1

    if not isinstance(user_config, dict):
        logger.error("Invalid config format in %s, expected JSON object", user_config_path)
        return -1

    role_key = ROLE_CONFIG_PATHS.get(role)
    if not role_key:
        logger.error("Invalid role: %s, must be one of %s", role, list(ROLE_CONFIG_PATHS))
        return -1

    role_config = get_val_by_key_path(user_config, role_key)

    if isinstance(role_config, dict):
        config = dict(role_config)
    else:
        # Fallback: treat USER_CONFIG_PATH as a raw role config
        config = dict(user_config)
        logger.warning("Role config '%s' not found, using raw config from %s", role_key, user_config_path)

    mgmt_tls_config = _get_mgmt_tls_config(user_config)
    if isinstance(mgmt_tls_config, dict) and MGMT_TLS_CONFIG not in config:
        config[MGMT_TLS_CONFIG] = mgmt_tls_config

    return config


def send_http_request(ip, port, url_path, config):
    """
    Send HTTP request to the probe endpoint.

    Args:
        ip: IP address
        port: Port number
        url_path: URL path (e.g., '/startup')

    Returns:
        True if successful, False otherwise
    """
    host_port = format_address(ip, port)
    url = f"http://{host_port}{url_path}"
    headers = {'User-Agent': 'sh-probe', 'Content-Type': 'application/json'}

    enable_tls = get_val_by_key_path(config, f'{MGMT_TLS_CONFIG}.{ENABLE_TLS}')

    try:
        if enable_tls:
            url = f"https://{host_port}{url_path}"

            cert_file = get_val_by_key_path(config, f'{MGMT_TLS_CONFIG}.{CERT_FILE}')
            key_file = get_val_by_key_path(config, f'{MGMT_TLS_CONFIG}.{KEY_FILE}')
            ca_file = get_val_by_key_path(config, f'{MGMT_TLS_CONFIG}.{CA_FILE}')
            password = get_val_by_key_path(config, f'{MGMT_TLS_CONFIG}.passwd_file')

            client = httpx.Client(
                headers=headers,
                timeout=TIMEOUT,
                cert=(cert_file, key_file, password if password else None),
                verify=ca_file,
            )
        else:
            client = httpx.Client(headers=headers, timeout=TIMEOUT)
        response = client.get(url)
        if response.status_code == 200:
            return True
        else:
            logger.error("HTTP request failed with status code: %s", response.status_code)
    except Exception as e:
        logger.error("Unexpected error: %s", e)

    return False


def main():
    """
    Main probe function.
    Usage: python probe.py <role> <probe_type>
    Where:
        role: 'controller', 'coordinator', 'prefill', 'decode', or 'union'
        probe_type: 'startup', 'readiness', or 'liveness'
    """
    if len(sys.argv) != 3:
        logger.error("Usage: python probe.py <role> <probe_type>")
        logger.error("  role: %s", list(ROLE_CONFIG_PATHS))
        logger.error("  probe_type: 'startup', 'readiness', or 'liveness'")
        sys.exit(1)

    role = sys.argv[1]
    probe_type = sys.argv[2]

    # Validate role
    if role not in ROLE_CONFIG_PATHS:
        logger.error("Invalid role: %s. Must be one of %s", role, list(ROLE_CONFIG_PATHS))
        sys.exit(1)

    # Validate probe_type
    if probe_type not in PROBE_URLS:
        logger.error("Invalid probe type: %s. Must be one of %s", probe_type, list(PROBE_URLS))
        sys.exit(1)

    # Get pod IP from environment
    pod_ip = os.environ.get('POD_IP')
    if not pod_ip:
        logger.error("POD_IP environment variable not set")
        sys.exit(1)

    config = get_config(role)
    logger.info("config: %s", config)
    if config == -1:
        logger.error("Failed to get config")
        sys.exit(1)

    if role in ENGINE_ROLES:
        role = 'node_manager'

    port_key = f'api_config.{role}_api_port'

    port = get_val_by_key_path(config, port_key)
    if not isinstance(port, int) or port < 1024 or port > 65535:
        logger.warning("Invalid port in config (%s=%s), using built-in default port", port_key, port)
        port = get_builtin_default_port(role)
        if port == -1:
            logger.error("Failed to get port")
            sys.exit(1)

    url_path = PROBE_URLS[probe_type]
    logger.info("Executing %s probe for %s at %s%s", probe_type, role, format_address(pod_ip, port), url_path)
    success = send_http_request(pod_ip, port, url_path, config)

    if success:
        logger.info("Service is %s", probe_type)
        sys.exit(0)  # success
    else:
        logger.error("Service is not %s", probe_type)
        sys.exit(1)  # failure


if __name__ == "__main__":
    main()
