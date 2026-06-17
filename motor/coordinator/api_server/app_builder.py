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
FastAPI app shell: management / observability / inference, CORS. No business routes;
ManagementServer / ObservabilityServer / InferenceServer register after getting app.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig

logger = get_logger(__name__)

_CORS_CONFIG = {
    "allow_origins": ["*"],
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}


class AppBuilder:
    """
    Create FastAPI app shell (with CORS); does not register business routes.
    """

    def __init__(self, config: CoordinatorConfig) -> None:
        self._config = config  # Not read by create_*_app; kept for API consistency / future use

    @staticmethod
    def create_management_app(
        lifespan: Callable[..., Any] | None = None,
    ) -> FastAPI:
        """Create Management FastAPI and add CORS."""
        app = FastAPI(
            title="Motor Coordinator Management Server",
            description="Management plane: liveness, startup, readiness, instance refresh",
            version="1.0.0",
            lifespan=lifespan,
        )
        app.add_middleware(CORSMiddleware, **_CORS_CONFIG)
        return app

    @staticmethod
    def create_observability_app(
        lifespan: Callable[..., Any] | None = None,
    ) -> FastAPI:
        """Create Observability FastAPI and add CORS."""
        app = FastAPI(
            title="Motor Coordinator Observability Server",
            description="Observability plane: metrics, health, request telemetry",
            version="1.0.0",
            lifespan=lifespan,
        )
        app.add_middleware(CORSMiddleware, **_CORS_CONFIG)
        return app

    @staticmethod
    def create_inference_app(
        lifespan: Callable[..., Any] | None = None,
    ) -> FastAPI:
        """
        Create Inference FastAPI (with CORS).
        Only called by InferenceServer (Worker process); Mgmt process runs
        ManagementServer and does not create inference_app.
        """
        app = FastAPI(
            title="Motor Coordinator Inference Server",
            description="Inference API endpoints (OpenAI-compatible and more)",
            version="1.0.0",
            lifespan=lifespan,
        )
        app.add_middleware(CORSMiddleware, **_CORS_CONFIG)
        return app
