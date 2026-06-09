# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""HTTP chat probe against decode endpoint (not daemon liveness in domain/probe.py)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from motor.common.http.http_client import HTTPClientPool
from motor.common.logger import get_logger
from motor.config.tls_config import TLSConfig

logger = get_logger(__name__)

PROBE_USER_QUESTION = "相对论的发明人是谁"
EXPECTED_ANSWER_SUBSTRING = "爱因斯坦"


@dataclass
class ProbeOutcome:
    failures: int
    details: list[str] = field(default_factory=list)


def _parse_infer_base_url(base_url: str) -> tuple[str, str]:
    u = urlparse(base_url.strip())
    host = u.hostname or ""
    if u.port:
        port = str(u.port)
    else:
        port = "443" if (u.scheme or "http").lower() == "https" else "80"
    return host, port


def _extract_completion_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    ch0 = choices[0] if isinstance(choices[0], dict) else {}
    message = ch0.get("message")
    if isinstance(message, dict) and message.get("content") is not None:
        return str(message.get("content") or "")
    if ch0.get("text") is not None:
        return str(ch0.get("text") or "")
    return json.dumps(ch0, ensure_ascii=False)


class ChatProbe(ABC):
    @abstractmethod
    async def run(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
        model: str,
        max_attempts: int,
        timeout_seconds: float,
    ) -> ProbeOutcome: ...


class FixedQAChatProbe(ChatProbe):
    """Direct HTTP to D endpoint (legacy / tests). Prefer InternalRouterProbe in production."""

    def __init__(
        self,
        infer_tls_config: TLSConfig,
        *,
        d_infer_base_url: str = "",
    ) -> None:
        self._tls = infer_tls_config
        self._d_infer_base_url = d_infer_base_url

    async def run(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
        model: str,
        max_attempts: int,
        timeout_seconds: float,
    ) -> ProbeOutcome:
        del p_instance_id, d_instance_id
        d_infer_base_url = self._d_infer_base_url
        if not d_infer_base_url.strip():
            logger.warning("FixedQAChatProbe: empty d_infer_base_url, all attempts failed")
            return ProbeOutcome(failures=max_attempts, details=["empty d_infer_base_url"])

        host, port = _parse_infer_base_url(d_infer_base_url)
        if not host:
            logger.warning("FixedQAChatProbe: could not parse host from %r", d_infer_base_url)
            return ProbeOutcome(failures=max_attempts, details=["unparseable base url"])

        failures = 0
        details: list[str] = []
        timeout = httpx.Timeout(timeout_seconds, connect=min(30.0, timeout_seconds))
        pool = HTTPClientPool()
        client = await pool.get_client(host, port, self._tls, timeout=timeout)

        payload = {
            "model": model.strip() or "default",
            "messages": [{"role": "user", "content": PROBE_USER_QUESTION}],
            "max_tokens": 64,
            "stream": False,
        }

        for attempt in range(max_attempts):
            try:
                resp = await client.post(
                    "/v1/chat/completions",
                    json=payload,
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                text = _extract_completion_text(data) if isinstance(data, dict) else ""
                if EXPECTED_ANSWER_SUBSTRING in text:
                    logger.info(
                        "FixedQAChatProbe: attempt %d/%d ok",
                        attempt + 1,
                        max_attempts,
                    )
                else:
                    logger.warning(
                        "FixedQAChatProbe: attempt %d/%d failed preview=%r",
                        attempt + 1,
                        max_attempts,
                        text[:200],
                    )
                    failures += 1
                    details.append(f"attempt {attempt + 1}: substring mismatch preview={text[:100]!r}")
            except Exception as exc:
                logger.warning(
                    "FixedQAChatProbe: attempt %d/%d error: %s",
                    attempt + 1,
                    max_attempts,
                    exc,
                )
                failures += 1
                details.append(f"attempt {attempt + 1}: {exc}")

        return ProbeOutcome(failures=failures, details=details)
