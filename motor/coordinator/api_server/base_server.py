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
Base class for Mgmt and Infer HTTP servers. Provides common config init, hot-reload,
uvicorn config, and timeout handler.
"""

import asyncio
import logging
import threading
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from fastapi import FastAPI, HTTPException, status

from motor.common.logger import ApiAccessFilter, get_logger
from motor.config.coordinator import CoordinatorConfig

logger = get_logger(__name__)

_UVICORN_GRACEFUL_SHUTDOWN = 30


class BaseCoordinatorServer:
    """Base class for Mgmt / Infer shared logic."""

    def __init__(self, config: CoordinatorConfig | None = None):
        self._config_lock = threading.RLock()
        self._initialize_config(config)
        self._service_start_timestamp = int(datetime.now(timezone.utc).timestamp())

    @staticmethod
    def create_base_uvicorn_config(app: FastAPI, host: str, port: int) -> dict[str, Any]:
        """Unified Uvicorn base config."""
        api_filter = ApiAccessFilter(
            {
                "/liveness": logging.ERROR,
                "/readiness": logging.ERROR,
            }
        )
        uvicorn_access_logger = logging.getLogger("uvicorn.access")
        uvicorn_access_logger.addFilter(api_filter)
        return {
            "app": app,
            "host": host,
            "port": port,
            "log_level": "info",
            "access_log": True,
            "lifespan": "on",
        }

    def apply_timeout_to_config(self, config_kwargs: dict[str, Any]) -> None:
        """Apply timeout to uvicorn config kwargs (in-place)."""
        config_kwargs["timeout_keep_alive"] = self.coordinator_config.exception_config.infer_timeout
        config_kwargs["timeout_graceful_shutdown"] = _UVICORN_GRACEFUL_SHUTDOWN

    def timeout_handler(self, timeout_seconds: float | None = None):
        """Unified timeout decorator."""

        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                actual_timeout = (
                    timeout_seconds
                    if timeout_seconds is not None
                    else self.coordinator_config.exception_config.infer_timeout
                )
                try:
                    return await asyncio.wait_for(func(*args, **kwargs), timeout=actual_timeout)
                except asyncio.TimeoutError as e:
                    logger.warning("Request timeout after %ss: %s", actual_timeout, func.__name__)
                    raise HTTPException(
                        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                        detail=f"Request timed out after {actual_timeout} seconds",
                    ) from e
                except HTTPException:
                    raise
                except Exception as e:
                    logger.error("Unexpected error in %s: %s", func.__name__, e, exc_info=True)
                    raise

            return wrapper

        return decorator

    def update_config(self, new_config: CoordinatorConfig) -> None:
        """Generic hot-reload skeleton; subclass overrides _apply_config_changes."""
        with self._config_lock:
            self.coordinator_config = new_config
            self._apply_config_changes(new_config)
        logger.info("%s configuration updated (hot-reload)", self.__class__.__name__)

    def _initialize_config(self, coordinator_config: CoordinatorConfig | None) -> None:
        """Subclass may override to load different defaults."""
        if coordinator_config is None:
            try:
                coordinator_config = CoordinatorConfig.from_json(None)
                logger.info("CoordinatorConfig loaded from file/env")
            except Exception as e:
                logger.error("Failed to load CoordinatorConfig: %s", e)
                raise RuntimeError("Failed to initialize CoordinatorConfig") from e
        self.coordinator_config = coordinator_config

    def _log_configuration(self) -> None:
        """Default logging implementation; subclass may override to add specific config."""
        logger.info("Service started at %s", self._service_start_timestamp)

    def _apply_config_changes(self, new_config: CoordinatorConfig) -> None:
        """Subclass implements concrete config application."""
        pass
