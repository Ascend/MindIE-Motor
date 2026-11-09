# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import logging

import pytest

import deploy as deploy_module


def test_main_reports_malformed_user_config_without_traceback(tmp_path, monkeypatch, caplog, capsys):
    (tmp_path / "user_config.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "env.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deploy_module.sys, "argv", ["deploy.py", "--config_dir", str(tmp_path)])

    with caplog.at_level(logging.ERROR, logger="lib.utils"):
        with pytest.raises(SystemExit) as exc_info:
            deploy_module.main()

    assert exc_info.value.code == 1
    assert "Failed to read user config" in caplog.text
    assert "Traceback" not in capsys.readouterr().err


def test_main_reports_non_object_user_config_without_traceback(tmp_path, monkeypatch, caplog, capsys):
    (tmp_path / "user_config.json").write_text("[]", encoding="utf-8")
    (tmp_path / "env.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deploy_module.sys, "argv", ["deploy.py", "--config_dir", str(tmp_path)])

    with caplog.at_level(logging.ERROR, logger="lib.utils"):
        with pytest.raises(SystemExit) as exc_info:
            deploy_module.main()

    assert exc_info.value.code == 1
    assert "expected a JSON object" in caplog.text
    assert "Traceback" not in capsys.readouterr().err


def test_main_reports_missing_config_dir_without_traceback(tmp_path, monkeypatch, caplog, capsys):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(deploy_module.sys, "argv", ["deploy.py", "--config_dir", str(missing)])

    with caplog.at_level(logging.ERROR, logger="lib.utils"):
        with pytest.raises(SystemExit) as exc_info:
            deploy_module.main()

    assert exc_info.value.code == 1
    assert "user_config.json not found in" in caplog.text
    assert "Traceback" not in capsys.readouterr().err
