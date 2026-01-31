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
import unittest
import signal
from unittest.mock import patch, MagicMock


from node_manager.daemon_manager.process_info import ProcessInfo


class TestLLMDaemonManager(unittest.TestCase):
    
    def setUp(self):
        self.path_check_patcher = patch('node_manager.common.utils.PathCheck.check_path_full')
        self.mock_path_check = self.path_check_patcher.start()
        self.mock_path_check.return_value = True  # Default to True for all path checks
        self.signal_patcher = patch('signal.signal')
        self.signal_patcher.start()
        from node_manager.daemon_manager.llm_daemon_starter import LLMDaemonManager
        if hasattr(LLMDaemonManager, 'reset_instance'):
            LLMDaemonManager.reset_instance()
        self.daemon_manager = LLMDaemonManager()
    
    def tearDown(self):
        self.path_check_patcher.stop()
        self.signal_patcher.stop()
    
    def create_mock_process(self, pid):
        mock_process = MagicMock()
        mock_process.pid = pid
        mock_process.poll.return_value = None
        return mock_process

    @patch('sys.argv', ['llm_daemon_starter.py', 'invalid_arg', 'pd'])
    def test_parse_arguments_error(self):
        with self.assertRaises(ValueError) as context:
            self.daemon_manager.parse_daemon_arguments()
        self.assertIn("Invalid arguments", str(context.exception))

    def test_path_check_failure(self):
        """Test that RuntimeError is raised when path validation fails"""
        self.mock_path_check.return_value = False
        from node_manager.daemon_manager.llm_daemon_starter import LLMDaemonManager
        if hasattr(LLMDaemonManager, 'reset_instance'):
            LLMDaemonManager.reset_instance()
        with self.assertRaises(RuntimeError) as context:
            self.daemon_manager.init_daemon_config()
        self.assertIn("Invalid", str(context.exception))

    @patch('subprocess.Popen')
    @patch('sys.argv', ['llm_daemon_starter.py'])
    def test_start_process_success(self, mock_popen):
        mock_popen.return_value = self.create_mock_process(12345)
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout="0-3\n")
            daemon_args = self.daemon_manager.parse_daemon_arguments()
            self.daemon_manager.start_daemon_instances(daemon_args)
            self.assertEqual(len(self.daemon_manager.process_info), 1)
            proc_info = self.daemon_manager.process_info[0]
            self.assertEqual(proc_info.name, "single_instance")
            self.assertTrue(proc_info.is_alive)

    @patch('os.waitpid')
    @patch('os.killpg')
    def test_child_process_signal_handling(self, mock_killpg, mock_waitpid):
        proc_info = ProcessInfo(
            process=self.create_mock_process(12345),
            name="test_process",
            config_file=None,
            command="mindie_llm_server"
        )
        self.daemon_manager.process_info.append(proc_info)
        mock_waitpid.side_effect = [
            (12345, 0),  # Process exited with code 0 (os.WEXITSTATUS(0) = 0)
            OSError()      # No more child processes
        ]
        with patch('os.WIFEXITED', return_value=True), \
             patch('os.WEXITSTATUS', return_value=0), \
             patch('os.WIFSIGNALED', return_value=False), \
             patch('os.WIFSTOPPED', return_value=False):
            self.daemon_manager.child_process_handler(signal.SIGCHLD, None)
            self.assertTrue(proc_info.has_exited)
            mock_killpg.assert_not_called()

    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('time.sleep')
    @patch('os.killpg')
    @patch('sys.argv', ['llm_daemon_starter.py', '2', 'decode'])
    def test_distributed_deployment_full_lifecycle(self, mock_killpg, mock_sleep, mock_run, mock_popen):
        mock_run.return_value = MagicMock(stdout="0-3\n")
        mock_processes = []
        for i in range(2):
            mock_process = self.create_mock_process(12345 + i)
            mock_processes.append(mock_process)
        mock_popen.side_effect = mock_processes
        wait_call_count = 0
        self.mock_path_check.return_value = True

        def mock_wait_for_completion():
            nonlocal wait_call_count
            wait_call_count += 1
            if wait_call_count == 1:
                # First call: simulate processes running for a while
                self.daemon_manager.terminate_all_processes()
                return -1
            return 0
        with patch.object(self.daemon_manager, '_wait_for_completion', side_effect=mock_wait_for_completion):
            result = self.daemon_manager.run()
            self.assertEqual(result, -1)
            self.assertEqual(len(self.daemon_manager.process_info), 2)
            # Verify CPU binding was called for decode role
            self.assertEqual(mock_run.call_count, 2)
            # Verify subprocess.Popen was called twice
            self.assertEqual(mock_popen.call_count, 2)
            mock_killpg.assert_called()
            self.assertTrue(self.daemon_manager.shutting_down)
            self.assertFalse(self.daemon_manager.running)


if __name__ == '__main__':
    unittest.main(verbosity=2)