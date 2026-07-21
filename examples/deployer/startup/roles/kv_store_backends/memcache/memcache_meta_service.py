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
import os

from memcache_hybrid import MetaService, MetaConfig


def _extract_port(url, default):
    """Extract port from a URL like tcp://host:port or http://host:port."""
    return url.rsplit(":", 1)[-1] if url else str(default)


def _format_address(host, port):
    """Format an IPv4, IPv6, or DNS host with a port."""
    raw_host = host.strip("[]")
    try:
        if isinstance(ipaddress.ip_address(raw_host), ipaddress.IPv6Address):
            return f"[{raw_host}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def main():
    pod_ip = os.environ.get("POD_IP", "127.0.0.1")

    # Always use Pod IP; only extract port from env vars if set
    config_store_port = _extract_port(os.environ.get("MMC_CONFIG_STORE_URL", ""), 50089)
    metrics_port = _extract_port(os.environ.get("MMC_METRICS_URL", ""), 50090)

    config = MetaConfig()
    config.meta_service_url = f"tcp://{_format_address(pod_ip, os.environ.get('KV_CACHE_STORE_PORT', '12345'))}"
    config.config_store_url = f"tcp://{_format_address(pod_ip, config_store_port)}"
    config.metrics_url = f"http://{_format_address(pod_ip, metrics_port)}"
    config.ha_enable = False
    config.log_level = "info"

    MetaService.setup(config)
    MetaService.main()


if __name__ == "__main__":
    main()
