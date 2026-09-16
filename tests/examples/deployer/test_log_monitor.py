# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import importlib.util


def _load_log_monitor():
    spec = importlib.util.spec_from_file_location("log_monitor", "examples/deployer/log_collect/log_monitor.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_failed_reconnect_keeps_existing_log(tmp_path):
    """A stream failure must not remove history collected before the failure."""
    module = _load_log_monitor()
    monitor = module.LogMonitor()
    monitor.exit_flag.set()
    module.g_target_log = str(tmp_path)
    log_file = tmp_path / "pod.log"
    log_file.write_text("history\n", encoding="utf-8")
    monitor._remove_failed_empty_log(str(log_file), existed_before_pull=True)

    assert log_file.read_text(encoding="utf-8") == "history\n"


def test_failed_new_empty_log_is_removed(tmp_path):
    """An empty file from a failed first pull should not consume a log slot."""
    module = _load_log_monitor()
    log_file = tmp_path / "pod.log"
    log_file.touch()
    module.LogMonitor._remove_failed_empty_log(str(log_file), existed_before_pull=False)
    assert not log_file.exists()
