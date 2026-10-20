# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from motor.common.utils.config_watcher import ConfigWatcher
from motor.config.controller import ControllerConfig


def test_watcher_reloads_atomically_replaced_config(tmp_path: Path) -> None:
    """Atomic configuration updates must trigger the same reload callbacks."""
    config_path = tmp_path / "user_config.json"
    config_path.write_text('{"value": 1}', encoding="utf-8")
    reloaded = threading.Event()
    reload_config = MagicMock(return_value=True)
    watcher = ConfigWatcher(str(config_path), reload_config, reloaded.set)
    watcher.start()
    try:
        replacement = tmp_path / "replacement.tmp"
        replacement.write_text('{"value": 2}', encoding="utf-8")
        os.replace(replacement, config_path)
        assert reloaded.wait(2), "Atomic replacement did not trigger a config reload"
        reload_config.assert_called_once()
    finally:
        watcher.stop()


def test_watcher_initialization():
    """Test ConfigWatcher initialization"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        config_path = f.name
        json.dump({"test": "data"}, f)

    try:
        watcher = ConfigWatcher(config_path, lambda: True)
        assert watcher.config_path == config_path
        assert watcher.reload_callback() is True
        assert watcher.debounce_seconds == 1.0
        assert watcher.observer is None
        assert watcher.event_handler is None
    finally:
        os.unlink(config_path)


def test_watcher_start_stop():
    """Test starting and stopping the watcher"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        config_path = f.name
        json.dump({"test": "data"}, f)

    try:
        watcher = ConfigWatcher(config_path, lambda: True)
        watcher.start()

        assert watcher.observer is not None
        assert watcher.event_handler is not None
        assert watcher.is_alive() is True

        watcher.stop()
        assert watcher.is_alive() is False
    finally:
        os.unlink(config_path)


def test_watcher_with_nonexistent_file():
    """Test watcher behavior with non-existent file"""
    config_path = "/tmp/nonexistent_config.json"
    watcher = ConfigWatcher(config_path, lambda: True)

    # Should not crash when starting with non-existent file
    watcher.start()
    assert watcher.observer is None  # Should not start observer

    watcher.stop()  # Should not crash


def test_config_reload_callback():
    """Test that reload callback is called when config changes"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        config_path = f.name
        json.dump({"key": "value1"}, f)

    reload_called = False

    def mock_reload():
        nonlocal reload_called
        reload_called = True
        return True

    try:
        watcher = ConfigWatcher(config_path, mock_reload, debounce_seconds=0.1)
        watcher.start()

        # Wait a bit for watcher to start
        time.sleep(0.05)

        # Modify the file
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump({"key": "value2"}, f)

        # Wait for reload
        time.sleep(0.05)

        assert reload_called is True

    finally:
        watcher.stop()
        os.unlink(config_path)


def test_watcher_with_controller_config():
    """Test watcher integration with ControllerConfig"""
    import uuid

    # Create a unique config file for this test to avoid parallel test interference
    unique_id = str(uuid.uuid4())[:8]
    temp_dir = tempfile.gettempdir()
    config_path = os.path.join(temp_dir, f"test_watcher_config_{unique_id}.json")
    watcher = None

    try:
        # Create initial config
        config_data = {
            "logging_config": {"log_level": "INFO"},
            "api_config": {"controller_api_host": "127.0.0.1", "controller_api_port": 8000},
        }

        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config_data, f)

        # Load config
        config = ControllerConfig.from_json(config_path)
        assert config.logging_config.log_level == "INFO"

        # Start watcher
        watcher = ConfigWatcher(config_path, config.reload, debounce_seconds=0.1)
        watcher.start()

        # Wait for watcher to start
        time.sleep(0.05)

        # Modify config
        config_data["logging_config"]["log_level"] = "DEBUG"
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config_data, f)

        # Wait for reload with retry logic
        max_attempts = 10
        reloaded = False
        for attempt in range(max_attempts):
            time.sleep(0.05)
            if config.logging_config.log_level == "DEBUG":
                reloaded = True
                break

        # Check if config was reloaded
        assert reloaded, (
            f"Config reload failed after {max_attempts} attempts. Current log_level: {config.logging_config.log_level}"
        )

    finally:
        if watcher:
            watcher.stop()
        try:
            os.unlink(config_path)
        except FileNotFoundError:
            pass


def test_watcher_with_update_callback():
    """Test ConfigWatcher initializes with update callback"""
    mock_callback = MagicMock()

    with patch('os.path.exists', return_value=True):
        watcher = ConfigWatcher(
            config_path="/fake/path.json",
            reload_callback=MagicMock(return_value=True),
            config_update_callback=mock_callback,
        )

        assert watcher.config_update_callback == mock_callback


def test_watcher_callback_execution():
    """Test ConfigWatcher calls update callback after successful reload"""
    mock_reload = MagicMock(return_value=True)
    mock_callback = MagicMock()

    with (
        patch('os.path.exists', return_value=True),
        patch('motor.common.utils.config_watcher.Observer'),
    ):
        # Create watcher
        fake_path = "/fake/path.json"
        watcher = ConfigWatcher(
            config_path=fake_path, reload_callback=mock_reload, config_update_callback=mock_callback
        )

        # Start watcher
        watcher.start()

        # Get the handler instance that was created
        handler_instance = watcher.event_handler
        assert handler_instance is not None

        # Call on_modified directly on the handler
        # The handler stores config_path as absolute path, so we need to match that
        mock_event = MagicMock()
        # Use the handler's config_path to ensure path matching works
        mock_event.src_path = handler_instance.config_path

        handler_instance.on_modified(mock_event)

        # Verify callback was called
        mock_callback.assert_called_once()


def test_watcher_with_config_update_integration():
    """Test end-to-end config file change triggers update callback"""
    # Create temporary config files
    with tempfile.TemporaryDirectory() as temp_dir:
        config_file = os.path.join(temp_dir, "test_config.json")

        # Initial config
        initial_config = {"logging_config": {"log_level": "INFO"}}

        # Updated config
        updated_config = {"logging_config": {"log_level": "DEBUG"}}

        # Write initial config
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(initial_config, f)

        # Create mock config object
        mock_config = MagicMock()
        mock_config.config_path = config_file
        mock_config.reload.return_value = True

        # Create mock callback
        mock_callback = MagicMock()

        with patch('os.path.exists', return_value=True):
            # Create and start watcher
            watcher = ConfigWatcher(
                config_path=config_file, reload_callback=mock_config.reload, config_update_callback=mock_callback
            )

            watcher.start()

            # Simulate file modification
            time.sleep(0.01)  # Reduced delay to ensure watcher is ready
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(updated_config, f)

            # Wait for watcher to detect change
            time.sleep(0.05)  # Reduced wait time

            # Stop watcher
            watcher.stop()

            # Verify callback was called
            mock_callback.assert_called()
