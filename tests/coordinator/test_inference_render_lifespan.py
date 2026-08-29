# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_server.inference_server import InferenceServer
from motor.coordinator.domain.request_manager import RequestManager

pytestmark = pytest.mark.anyio
MODULE = "motor.coordinator.api_server.inference_server"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def make_server():
    def build(render_enabled=False):
        config = CoordinatorConfig()
        config.render_config.enabled = render_enabled
        config.context_budget_mode = "on"
        config.aigw_model = {"p_max_seqlen": 2048, "d_max_seqlen": 1024}
        server = InferenceServer(config, request_manager=RequestManager(config))
        server._scheduler_connection.connect = AsyncMock()
        server._scheduler_connection.disconnect = AsyncMock()
        return server

    return build


@pytest.fixture
def lifespan_dependencies():
    render_client = MagicMock()
    render_client.health = AsyncMock(return_value=False)
    render_client.aclose = AsyncMock()
    tokenization_service = object()
    local_tokenizer = object()
    with (
        patch(f"{MODULE}.VLLMRenderClient", return_value=render_client),
        patch(f"{MODULE}.TokenizationService", return_value=tokenization_service),
        patch(f"{MODULE}.TokenizerManager", return_value=local_tokenizer),
        patch(f"{MODULE}.TracerManager"),
    ):
        yield SimpleNamespace(
            render_client=render_client,
            tokenization_service=tokenization_service,
        )


async def test_render_health_failure_does_not_block_inference_lifespan(make_server, lifespan_dependencies):
    server = make_server(render_enabled=True)
    deps = lifespan_dependencies

    async with server._lifespan(server.app):
        assert server.app.state.tokenization_service is deps.tokenization_service
        deps.render_client.health.assert_awaited_once()

    deps.render_client.aclose.assert_awaited_once()
    server._scheduler_connection.disconnect.assert_awaited_once()
