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
import re
import sys
import subprocess
from typing import List, Dict, Optional

from node_manager.common.utils import _SingletonMeta, PathCheck
from .base_daemon_manager import BaseDaemonManager


class LLMDaemonManager(BaseDaemonManager, metaclass=_SingletonMeta):

    def __init__(self):
        super().__init__(daemon_type="MindIE-LLM")

    def init_daemon_config(self):
        self.mies_install_path = os.getenv('MIES_INSTALL_PATH', '.')
        self.config_dir = os.path.join(self.mies_install_path, 'conf')

        if not PathCheck.check_path_full(self.mies_install_path):
            raise RuntimeError(f"Invalid MIES_INSTALL_PATH: {self.mies_install_path}")
        if not PathCheck.check_path_full(self.config_dir, mode=0o750):
            raise RuntimeError(f"Invalid config directory: {self.config_dir}")

    def build_daemon_command(self, config_file: Optional[str], instance_name: str, **kwargs) -> List[str]:
        cpu_binding = kwargs.get('cpu_binding')
        if cpu_binding:
            cmd = ['taskset', '-c', cpu_binding, "$MINDIE_USER_HOME_PATH/bin/mindie_llm_server"]
        else:
            cmd = ["$MINDIE_USER_HOME_PATH/bin/mindie_llm_server"]
        if config_file:
            cmd.extend(['--config-file', config_file])
        cmd.extend(['--expert-parallel', 'true'])
        return cmd

    def parse_daemon_arguments(self) -> Dict:
        if len(sys.argv) == 1:
            return {'mode': 'single'}
        elif len(sys.argv) == 3:
            # Distributed mode: server_count role
            try:
                server_count = int(sys.argv[1])
                role = sys.argv[2]
                if server_count <= 0:
                    raise ValueError("Server count must be positive")
                if role not in ["decode", "prefill"]:
                    raise ValueError("Role must be 'decode' or 'prefill'")
                return {
                    'mode': 'distributed',
                    'server_count': server_count,
                    'role': role
                }
            except ValueError as e:
                raise ValueError(f"Invalid arguments: {e}") from e
        else:
            raise ValueError("Invalid arguments. Usage: node_manager [server_count role]")

    def get_device_type(self) -> str:
        command = "lspci"
        result = subprocess.run([command], capture_output=True, text=True, check=False, timeout=5)
        if result.returncode != 0:
            self.logger.warning(f"lspci command failed with return code {result.returncode}")
            return None
        pci_type = None
        device_pattern = re.compile(r"Device\s+(d\d{3})", re.IGNORECASE)
        for line in result.stdout.splitlines():
            if "accelerators" in line:
                match = device_pattern.search(line)
                if match:
                    pci_type = match.group(1)
                    break
        pci_type_to_device_type = {"d802": "800i_a2", "d803": "800i_a3"}
        return pci_type_to_device_type.get(pci_type, None)

    def get_cpu_binding(self, instance_id: int) -> Optional[str]:
        binding_file = os.path.join(self.mies_install_path,
                                    'examples/kubernetes_deploy_scripts/boot_helper/mindie_cpu_binding.py')
        if not PathCheck.check_path_full(binding_file, mode=0o550):
            raise RuntimeError(f"Invalid CPU binding script: {binding_file}")
        device_type = self.get_device_type()
        if not device_type:
            self.logger.warning("Unable to determine device type for CPU binding")
            return None
        try:
            cmd = [
                'python3',
                binding_file,
                str(instance_id),
                '--device_type', device_type
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except Exception as e:
            self.logger.error(f"Failed to get CPU binding for instance {instance_id}: {e}")
            return None

    def start_daemon_instances(self, daemon_args: Dict):
        if daemon_args['mode'] == 'single':
            self.start_single_mode()
        elif daemon_args['mode'] == 'distributed':
            self.start_distributed_mode(
                daemon_args['server_count'],
                daemon_args['role']
            )

    def start_distributed_mode(self, server_count: int, role: str):
        for i in range(1, server_count + 1):
            config_file = os.path.join(self.config_dir, f'config{i}.json')
            if not PathCheck.check_path_full(config_file, mode=0o640):
                raise RuntimeError(f"Invalid config file: {config_file}")
            if role == "decode":
                # decode mode requires CPU binding
                cpu_binding = self.get_cpu_binding(i)
                instance_name = f"D_instance_{i}"
            else:
                cpu_binding = None
                instance_name = f"P_instance_{i}"
            self.start_daemon_process(
                config_file,
                instance_name,
                cpu_binding=cpu_binding,
                working_dir=self.mies_install_path
            )

    def start_single_mode(self):
        config_file = os.path.join(self.config_dir, 'config.json')
        self.start_daemon_process(
            config_file,
            "single_instance",
            working_dir=self.mies_install_path
        )


llm_daemon_manager = LLMDaemonManager()