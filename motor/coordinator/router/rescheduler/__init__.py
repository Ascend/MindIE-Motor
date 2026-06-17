# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared helpers for PD/CDP rescheduler (token-cache retry and request rewrite).

Performance note: per-chunk JSON re-serialization is CPU-bound; ``async`` does not
make it faster. Mitigations here use :mod:`msgspec` (already a dependency) for
compact JSON bytes and fewer temporary allocations on hot paths. Offloading
serialization to ``asyncio.to_thread`` is reserved for unusually large payloads
where event-loop latency matters; typical SSE chunks are cheaper inline.
"""

__all__ = [
    "Rescheduler",
]

from .rescheduler import Rescheduler
