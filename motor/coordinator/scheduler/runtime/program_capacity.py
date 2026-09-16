# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# See the Mulan PSL v2 for more details.

"""Small vLLM metrics adapter used by Program admission."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from motor.coordinator.api_client.native_engine_api_client import NativeEngineApiClient


_CACHE_CONFIG_RE = re.compile(r"^vllm:cache_config_info\{(?P<labels>[^}]*)\}\s+(?P<value>[-+0-9.eE]+)")
_USAGE_RE = re.compile(
    r"^vllm:(?:kv_cache_usage_perc|gpu_cache_usage_perc)\{(?P<labels>[^}]*)\}\s+"
    r"(?P<value>[-+0-9.eE]+)"
)
_LABEL_RE = re.compile(r'(?P<key>[A-Za-z_][A-Za-z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class ParsedCapacity:
    """Parsed logical capacity and current physical usage."""

    total_kv_tokens: int
    native_used_kv_tokens: int
    native_waiting_kv_tokens: int = 0


def _labels(raw: str) -> dict[str, str]:
    return {match.group("key"): match.group("value") for match in _LABEL_RE.finditer(raw)}


def parse_vllm_capacity(metrics_text: str, fallback_total_kv_tokens: int = 0) -> ParsedCapacity | None:
    """Parse one engine metrics response; return ``None`` when capacity is unknown."""
    total_tokens = 0
    usage_ratio: float | None = None
    for line in metrics_text.splitlines():
        cache_match = _CACHE_CONFIG_RE.match(line.strip())
        if cache_match:
            labels = _labels(cache_match.group("labels"))
            try:
                blocks = int(float(labels.get("num_gpu_blocks", "0")))
                block_size = int(float(labels.get("block_size", "0")))
            except (TypeError, ValueError):
                blocks = block_size = 0
            if blocks > 0 and block_size > 0:
                total_tokens = blocks * block_size
            continue
        usage_match = _USAGE_RE.match(line.strip())
        if usage_match:
            try:
                usage_ratio = max(0.0, float(usage_match.group("value")))
            except (TypeError, ValueError):
                usage_ratio = None
    if total_tokens <= 0:
        total_tokens = max(0, int(fallback_total_kv_tokens))
    if total_tokens <= 0:
        return None
    used_tokens = int(total_tokens * usage_ratio) if usage_ratio is not None else 0
    return ParsedCapacity(total_tokens, max(0, min(total_tokens, used_tokens)))


class ProgramCapacityProvider:
    """Non-blocking endpoint metrics reader with a static capacity fallback."""

    def __init__(self, fallback_total_kv_tokens: int = 0) -> None:
        self._fallback_total_kv_tokens = max(0, fallback_total_kv_tokens)

    async def read(self, address: str, tls_config=None) -> ParsedCapacity | None:
        """Read one native endpoint without blocking the Scheduler event loop."""
        metrics = await asyncio.to_thread(NativeEngineApiClient.query_metrics, address, tls_config)
        return parse_vllm_capacity(metrics, self._fallback_total_kv_tokens)
