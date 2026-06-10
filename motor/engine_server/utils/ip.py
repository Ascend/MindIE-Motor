#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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


def ip_valid_check(ip_str: str) -> None:
    try:
        parsed_ip = ipaddress.ip_address(ip_str)
    except ValueError as e:
        raise ValueError(f"{ip_str} parse to ip failed") from e

    if parsed_ip.is_unspecified:
        raise ValueError(f"{ip_str} is all zeros ip")

    if parsed_ip.is_multicast:
        raise ValueError(f"{ip_str} is multicast ip")


def port_valid_check(port: int) -> None:
    if port < 1024 or port > 65535:
        raise ValueError(f"{port} port must be between 1024 and 65535")


def is_valid_ipv6_address(address: str | None) -> bool:
    if not address or not isinstance(address, str):
        return False
    try:
        ipaddress.IPv6Address(address)
        return True
    except (ValueError, TypeError):
        return False


def build_endpoint(ip: str | None, port: int) -> str:
    if not ip:
        raise ValueError("ip must not be empty")
    if is_valid_ipv6_address(ip):
        return f"[{ip}]:{port}"
    return f"{ip}:{port}"
