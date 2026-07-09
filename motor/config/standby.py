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

LOCK_SLASH = "/"


@dataclass
class StandbyConfig:
    """Standby (master/standby) configuration class"""

    # master/standby feature enable/disable
    enable_master_standby: bool = False

    # master/standby check interval in seconds
    master_standby_check_interval: int = 5

    # master lock lease TTL in seconds
    # Raised from 10s to 15s to provide headroom for transient etcd/network
    # issues while keeping failover recovery time acceptable.
    master_lock_ttl: int = 15

    # master lock retry interval in seconds
    master_lock_retry_interval: int = 5

    # max consecutive lock failures
    master_lock_max_failures: int = 3

    # master lock key path in ETCD
    # Note: controller and coordinator will automatically
    # add "/controller/" and "/coordinator/" prefixes respectively
    master_lock_key: str = "/master_lock"
