#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import sys

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from node_manager.core.runtime_param_checker import (
    RuntimeParamChecker,
    runtime_param_checker,
)


class TestRuntimeParamChecker(unittest.TestCase):

    def tearDown(self):
        """每个用例结束后恢复单例状态，避免用例间相互影响"""
        runtime_param_checker.stop_event.clear()

    @patch.dict(os.environ, {"MODEL_NAME": "TestModel"}, clear=False)
    def test_get_model_name_with_non_empty_env(self):
        """MODEL_NAME不为空时返回全小写字符串"""
        self.assertEqual(RuntimeParamChecker.get_model_name(), "testmodel")

    @patch.dict(os.environ, {"MODEL_NAME": ""}, clear=False)
    def test_get_model_name_without_empty_env(self):
        """MODEL_NAME为空时返回空串"""
        self.assertEqual(RuntimeParamChecker.get_model_name(), "")

    @patch.dict(os.environ, {}, clear=False)
    def test_get_model_name_with_missing_env(self):
        """MODEL_NAME环境变量不存在时返回空串"""
        self.assertEqual(RuntimeParamChecker.get_model_name(), "")

    @patch.dict(os.environ, {"MINDIE_LOG_PATH": "/tmp/mindie_log"}, clear=False)
    def test_get_output_dir_with_env(self):
        """MINDIE_LOG_PATH不为空时返回指定路径"""
        path = RuntimeParamChecker.get_output_dir()
        self.assertEqual(path, os.path.join("/tmp/mindie_log", "runtime_param_check"))

    @patch.dict(os.environ, {}, clear=False)
    def test_get_output_dir_with_missing_env(self):
        """MINDIE_LOG_PATH环境变量不存在时返回默认路径"""
        path = RuntimeParamChecker.get_output_dir()
        self.assertTrue(path.endswith(os.path.join("runtime_param_check")))
        self.assertIn("mindie", path)

    def test_get_next_check_time_cross_day(self):
        """当前时间已过检查点时应顺延到次日"""
        fake_now_utc = datetime(2025, 2, 26, 15, 30, 0, tzinfo=timezone.utc)
        fake_now = fake_now_utc.astimezone(timezone(timedelta(hours=8)))
        mock_now = MagicMock()
        mock_now.astimezone.return_value = fake_now
        with patch("node_manager.core.runtime_param_checker.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            with patch.object(runtime_param_checker, "check_time", 23):
                next_time = runtime_param_checker.get_next_check_time()
            mock_dt.now.assert_called_once_with(timezone.utc)
        self.assertEqual(next_time.date().day, 27)
        self.assertEqual(next_time.hour, 23)
        self.assertEqual(next_time.minute, 0)

    def test_get_next_check_time_same_day(self):
        """当前时间未到检查点时应为当日"""
        fake_now_utc = datetime(2025, 2, 26, 14, 0, 0, tzinfo=timezone.utc)
        fake_now = fake_now_utc.astimezone(timezone(timedelta(hours=8)))
        mock_now = MagicMock()
        mock_now.astimezone.return_value = fake_now
        with patch("node_manager.core.runtime_param_checker.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            with patch.object(runtime_param_checker, "check_time", 23):
                next_time = runtime_param_checker.get_next_check_time()
            mock_dt.now.assert_called_once_with(timezone.utc)
        self.assertEqual(next_time.date().day, 26)
        self.assertEqual(next_time.hour, 23)
        self.assertEqual(next_time.minute, 0)

    def test_rotate_check_results_keeps_only_newest_backup_count(self):
        """轮转后仅保留最新backup_count个msprechecker*.json文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(5):
                fpath = os.path.join(tmpdir, f"msprechecker_{i}.json")
                with open(fpath, "w") as f:
                    f.write("{}")
                if i > 0:
                    # 模拟文件创建时间差异
                    time.sleep(0.02)
            runtime_param_checker.rotate_check_results(output_dir=tmpdir, backup_count=3)
            remaining = [
                f
                for f in os.listdir(tmpdir)
                if f.startswith("msprechecker") and f.endswith(".json")
            ]
            self.assertEqual(len(remaining), 3)
            self.assertNotIn("msprechecker_0.json", remaining)
            self.assertNotIn("msprechecker_1.json", remaining)
            self.assertIn("msprechecker_2.json", remaining)
            self.assertIn("msprechecker_3.json", remaining)
            self.assertIn("msprechecker_4.json", remaining)

    def test_rotate_check_results_nothing_to_do_when_within_backup_count(self):
        """文件数不超过backup_count时, 不删除文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                with open(os.path.join(tmpdir, f"msprechecker_{i}.json"), "w") as f:
                    f.write("{}")
            runtime_param_checker.rotate_check_results(output_dir=tmpdir, backup_count=10)
            remaining = [
                f
                for f in os.listdir(tmpdir)
                if f.startswith("msprechecker") and f.endswith(".json")
            ]
            self.assertEqual(len(remaining), 3)

    def test_rotate_check_results_ignores_non_matching_files(self):
        """只轮转msprechecker*.json文件, 其它文件不动"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "msprechecker_old.json"), "w") as f:
                f.write("{}")
            time.sleep(0.02)
            with open(os.path.join(tmpdir, "msprechecker_new.json"), "w") as f:
                f.write("{}")
            with open(os.path.join(tmpdir, "other.txt"), "w") as f:
                f.write("x")
            runtime_param_checker.rotate_check_results(output_dir=tmpdir, backup_count=1)
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "msprechecker_old.json")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "msprechecker_new.json")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "other.txt")))

    def test_check_loop_calls_run_check_when_time_reached(self):
        """check_loop在到达next_check_time时, 调用run_check"""
        past_time = datetime.now(timezone.utc).astimezone() - timedelta(seconds=10)
        with patch.object(runtime_param_checker, "get_next_check_time", return_value=past_time):
            with patch.object(runtime_param_checker, "stop_event") as mock_event:
                mock_event.is_set.side_effect = [False, True]
                with patch.object(runtime_param_checker, "run_check") as mock_run_check:
                    runtime_param_checker.check_loop()
                    mock_run_check.assert_called_once()

    def test_check_loop_exits_when_stop_event_set(self):
        """check_loop在stop_event被设置时立即退出, 不调用run_check"""
        with patch.object(runtime_param_checker, "stop_event") as mock_event:
            mock_event.is_set.return_value = True
            with patch.object(runtime_param_checker, "run_check") as mock_run_check:
                runtime_param_checker.check_loop()
                mock_run_check.assert_not_called()

    def test_run_when_not_initialized_returns_early(self):
        with patch.object(runtime_param_checker, "initialized_success", False):
            with patch.object(runtime_param_checker, "run_check") as mock_run:
                runtime_param_checker.run()
                mock_run.assert_not_called()

    def test_stop_sets_stop_event(self):
        runtime_param_checker.stop_event.clear()
        runtime_param_checker.stop()
        self.assertTrue(runtime_param_checker.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
