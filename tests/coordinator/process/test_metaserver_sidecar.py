# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Metaserver bind/serve failure must not take down the infer Worker."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("uvloop", MagicMock())

from motor.config.coordinator import CoordinatorConfig  # noqa: E402
from motor.coordinator.process.inference_manager import (  # noqa: E402
    run_inference_and_metaserver,
    serve_worker_metaserver,
)


class _HangServer:
    should_exit = False

    def __init__(self):
        self.cancelled = asyncio.Event()

    async def serve(self, sockets=None):
        del sockets
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _FailingServer:
    should_exit = False

    async def serve(self, sockets=None):
        del sockets
        raise OSError("Address already in use")


class _ReleaseServer:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def serve(self, sockets=None):
        del sockets
        self.started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_serve_worker_metaserver_clears_port_on_bind_error(caplog):
    config = CoordinatorConfig()
    config.worker_metaserver_port = 12000

    await serve_worker_metaserver(_FailingServer(), config, 0, "127.0.0.1", 12000)

    assert config.worker_metaserver_port is None
    assert any("metaserver failed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_metaserver_serve_failure_does_not_stop_infer():
    config = CoordinatorConfig()
    config.worker_metaserver_port = 12000
    infer = _ReleaseServer()

    task = asyncio.create_task(run_inference_and_metaserver(infer, None, _FailingServer(), config, 0, "127.0.0.1"))
    await infer.started.wait()
    for _ in range(50):
        if config.worker_metaserver_port is None:
            break
        await asyncio.sleep(0)
    assert config.worker_metaserver_port is None
    infer.release.set()
    await task


@pytest.mark.asyncio
async def test_infer_exit_stops_metaserver_without_clearing_port():
    config = CoordinatorConfig()
    config.worker_metaserver_port = 12001
    infer = _ReleaseServer()
    meta = _HangServer()

    task = asyncio.create_task(run_inference_and_metaserver(infer, None, meta, config, 1, "10.0.0.1"))
    await infer.started.wait()
    infer.release.set()
    await task

    assert meta.cancelled.is_set()
    assert config.worker_metaserver_port == 12001


@pytest.mark.asyncio
async def test_infer_failure_stops_metaserver_and_raises():
    config = CoordinatorConfig()
    config.worker_metaserver_port = 12002
    meta = _HangServer()

    class _BoomInfer:
        async def serve(self, sockets=None):
            del sockets
            raise RuntimeError("infer boom")

    with pytest.raises(RuntimeError, match="infer boom"):
        await run_inference_and_metaserver(_BoomInfer(), None, meta, config, 2, "10.0.0.1")

    assert meta.cancelled.is_set()
    assert config.worker_metaserver_port == 12002
