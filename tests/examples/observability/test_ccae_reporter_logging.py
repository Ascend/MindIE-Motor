# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import importlib
import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


@pytest.fixture
def reporter_logging(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "examples/features/observability"))
    return importlib.import_module("ccae_reporter.common.logging")


@pytest.mark.parametrize("identity", [None, "controller", "my-controller", "my.controller"])
def test_collect_only_active_logs(
    reporter_logging: ModuleType, monkeypatch: pytest.MonkeyPatch, identity: str | None
) -> None:
    collector = importlib.import_module("ccae_reporter.backends.log_collect.log_collector")
    monkeypatch.setattr(collector, "Log", Mock())
    monkeypatch.setattr(collector.PathCheck, "check_path_full", lambda path: True)
    handler = collector.CollectHandler(identity)
    component = identity or "coordinator"
    filename = f"/logs/motor-{component}-abc-123.log"
    for suffix in ["", ".1", ".tmp", ".gz"]:
        path = filename + suffix
        handler.on_created(Mock(src_path=path, is_directory=False))
        assert (path in handler.log_processor.watch_files) == (suffix == "")
    for name in ["motor-controller-abc_123.log", "motor-other-abc.log", "motor-myXcontroller-abc.log"]:
        handler.on_created(Mock(src_path=name, is_directory=False))
        assert name not in handler.log_processor.watch_files
    directory = f"/logs/dir-{component}-abc.log"
    handler.on_created(Mock(src_path=directory, is_directory=True))
    assert len(handler.log_processor.watch_files) == 1


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "INFO"),
        ("", "INFO"),
        (" debug ", "DEBUG"),
        ("WARNING", "WARN"),
        ("WARN", "WARN"),
        ("INFO", "INFO"),
        ("ERROR", "ERROR"),
        ("CRITICAL", "CRITICAL"),
        ("NULL", "NULL"),
        ("bogus", "INFO"),
    ],
)
def test_log_level_configuration(
    reporter_logging: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
    expected: str,
) -> None:
    monkeypatch.delenv("MOTOR_LOG_PATH", raising=False)
    if value is None:
        monkeypatch.delenv("CCAE_REPORTER_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("CCAE_REPORTER_LOG_LEVEL", value)
    with caplog.at_level(logging.WARNING):
        params = reporter_logging.init_logger()
    assert params.level == expected
    assert params.path == "/root/ascend/log"
    warnings = [record for record in caplog.records if "CCAE_REPORTER_LOG_LEVEL" in record.getMessage()]
    assert len(warnings) == int(value == "bogus")
    if warnings:
        assert warnings[0].levelno == logging.WARNING
        assert "BOGUS" in warnings[0].getMessage()
        assert "INFO" in warnings[0].getMessage()
