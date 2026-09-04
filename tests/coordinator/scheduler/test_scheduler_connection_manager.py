# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for SchedulerConnectionManager"""

import unittest
from unittest.mock import Mock, AsyncMock, patch
import asyncio

from motor.coordinator.scheduler.runtime.scheduler_connection_manager import (
    SchedulerConnectionManager,
    _CONNECT_MAX_RETRIES,
)


class TestSchedulerConnectionManager(unittest.TestCase):
    """Tests for SchedulerConnectionManager connect/disconnect lifecycle."""

    def setUp(self):
        self.mock_client = AsyncMock()
        self.mock_client_config = Mock()
        self.manager = SchedulerConnectionManager(
            client=self.mock_client,
            client_config=self.mock_client_config,
        )

    def test_init(self):
        """Manager starts disconnected with correct client reference."""
        self.assertFalse(self.manager._connected)
        self.assertIs(self.manager._client, self.mock_client)

    def test_connect_success(self):
        """Connect succeeds when client.connect returns True."""
        self.mock_client.connect = AsyncMock(return_value=True)
        asyncio.run(self.manager.connect())
        self.assertTrue(self.manager._connected)
        self.mock_client.connect.assert_called_once()

    def test_connect_failure_then_retry_success(self):
        """Connect retries on failure and eventually succeeds."""
        self.mock_client.connect = AsyncMock(side_effect=[False, True])
        with patch(
            "motor.coordinator.scheduler.runtime.scheduler_connection_manager.asyncio.sleep",
            return_value=None,
        ):
            asyncio.run(self.manager.connect())
        self.assertTrue(self.manager._connected)
        self.assertEqual(self.mock_client.connect.call_count, 2)

    def test_connect_all_retries_fail(self):
        """Connect exhausts all retries without success (background reconnect cancelled below)."""
        self.mock_client.connect = AsyncMock(return_value=False)

        async def scenario():
            await self.manager.connect()
            task = self.manager._reconnect_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        with patch(
            "motor.coordinator.scheduler.runtime.scheduler_connection_manager.asyncio.sleep",
            return_value=None,
        ):
            asyncio.run(scenario())
        self.assertFalse(self.manager._connected)
        self.assertEqual(self.mock_client.connect.call_count, _CONNECT_MAX_RETRIES)

    def test_connect_with_exception(self):
        """Connect retries after an exception and eventually succeeds."""
        self.mock_client.connect = AsyncMock(side_effect=[Exception("Connection refused"), True])
        with patch(
            "motor.coordinator.scheduler.runtime.scheduler_connection_manager.asyncio.sleep",
            return_value=None,
        ):
            asyncio.run(self.manager.connect())
        self.assertTrue(self.manager._connected)
        self.assertEqual(self.mock_client.connect.call_count, 2)

    def test_connect_no_client(self):
        """Connect returns early without error when no client is set."""
        self.manager._client = None
        asyncio.run(self.manager.connect())
        # No exception should be raised

    def test_disconnect(self):
        """Disconnect calls client.disconnect and marks as disconnected."""
        self.manager._connected = True
        asyncio.run(self.manager.disconnect())
        self.mock_client.disconnect.assert_called_once()
        self.assertFalse(self.manager._connected)

    def test_disconnect_not_connected(self):
        """Disconnect does nothing when not connected."""
        self.manager._connected = False
        asyncio.run(self.manager.disconnect())
        self.mock_client.disconnect.assert_not_called()

    def test_disconnect_idempotent(self):
        """Disconnect is idempotent: second call is a no-op."""
        self.manager._connected = True
        asyncio.run(self.manager.disconnect())
        asyncio.run(self.manager.disconnect())
        self.mock_client.disconnect.assert_called_once()

    def test_ensure_connected_when_already(self):
        """ensure_connected does nothing when already connected."""
        self.manager._connected = True
        asyncio.run(self.manager.ensure_connected())
        self.mock_client.connect.assert_not_called()

    def test_ensure_connected_when_not(self):
        """ensure_connected delegates to connect when not connected."""
        self.manager._connected = False
        self.mock_client.connect = AsyncMock(return_value=True)
        asyncio.run(self.manager.ensure_connected())
        self.mock_client.connect.assert_called_once()

    def test_get_client_connected(self):
        """get_client returns the client when connected."""
        self.manager._connected = True
        self.assertIs(self.manager.get_client(), self.mock_client)

    def test_get_client_not_connected(self):
        """get_client returns None when not connected."""
        self.manager._connected = False
        self.assertIsNone(self.manager.get_client())

    def test_connect_all_retries_fail_starts_background_reconnect(self):
        """After the initial retries are exhausted, a background task keeps retrying until success."""
        self.mock_client.connect = AsyncMock(side_effect=[False, False, False, True])

        async def scenario():
            await self.manager.connect()
            self.assertFalse(self.manager._connected)
            self.assertIsNotNone(self.manager._reconnect_task)
            await self.manager._reconnect_task

        with patch(
            "motor.coordinator.scheduler.runtime.scheduler_connection_manager.asyncio.sleep",
            return_value=None,
        ):
            asyncio.run(scenario())
        self.assertTrue(self.manager._connected)
        self.assertEqual(self.mock_client.connect.call_count, 4)

    def test_disconnect_cancels_background_reconnect_task(self):
        """disconnect() must not leave an orphaned reconnect loop retrying forever."""
        self.mock_client.connect = AsyncMock(return_value=False)

        async def scenario():
            await self.manager.connect()
            task = self.manager._reconnect_task
            self.assertIsNotNone(task)
            await self.manager.disconnect()
            self.assertIsNone(self.manager._reconnect_task)
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.assertTrue(task.done())

        with patch(
            "motor.coordinator.scheduler.runtime.scheduler_connection_manager.asyncio.sleep",
            return_value=None,
        ):
            asyncio.run(scenario())
