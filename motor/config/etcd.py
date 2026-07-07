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

from dataclasses import dataclass


@dataclass
class EtcdConfig:
    """ETCD configuration class"""

    # ETCD connection configuration
    etcd_host: str = 'etcd.default.svc.cluster.local'
    etcd_port: int = 2379
    etcd_timeout: int = 5
    etcd_lb_policy: str = 'round_robin'

    # ETCD persistence configuration
    enable_etcd_persistence: bool = False  # Enable/disable ETCD persistence and restoration
