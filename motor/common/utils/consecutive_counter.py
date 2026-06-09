# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Per-key consecutive hit counter with threshold (generic util, no domain semantics)."""

from __future__ import annotations

import asyncio
from typing import Hashable

PDGroupKey = tuple[int | None, int]


class ConsecutiveCounter:
    """Count consecutive ``hit=True`` per key; reset on ``hit=False``."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold
        self._counts: dict[Hashable, int] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}

    def _lock(self, key: Hashable) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def record(self, key: Hashable, hit: bool) -> bool:
        """Record one outcome. Returns True if consecutive hits reached threshold."""
        async with self._lock(key):
            if hit:
                self._counts[key] = self._counts.get(key, 0) + 1
                return self._counts[key] >= self._threshold
            self._counts[key] = 0
            return False

    async def reset(self, key: Hashable) -> None:
        async with self._lock(key):
            self._counts[key] = 0

    def get_count(self, key: Hashable) -> int:
        return self._counts.get(key, 0)
