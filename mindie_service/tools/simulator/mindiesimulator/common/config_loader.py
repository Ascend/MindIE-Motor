#!/usr/bin/python3.11
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import json
import re
import subprocess
import shutil
from pathlib import Path

from .path_check import PathCheck

VERSION_KEY = "mindie-service"
HBM_32G = 32
HBM_64G = 64


def input_validate(input_str):
    if len(input_str) > 100:
        raise ValueError(f"Too long input string {input_str}, please check what you input.")

    digits = re.sub(r"[^\d]", "", input_str)

    if not digits:
        raise ValueError(f"Please enter a valid input string, {input_str} must contain necessary digits.")

    return int(digits)


class ConfigLoader:
    def __init__(self):
        self.version_info = None
        self.service_config = None
        self.model_config = None
        self.model_params = None
        self.model_name = None
        self.model_config_path = None
        self.llm_size = None
        self.npu_name = None
        self.world_size = None
        self.hbm_size = None

        self.hardware_params = None

        self.config_file_path = None
        self.version_info_path = None

        self.get_config_file_path()
        self.get_version_info_path()

        self.possible_hidden_size_keys = ['hidden_size']
        self.possible_num_hidden_layers_keys = ['num_hidden_layers']
        self.possible_num_attention_heads_keys = ['num_attention_heads']
        self.possible_num_key_value_heads_keys = ['num_key_value_heads']

        self.version_info = self.extract_version()
        self.service_config = self.load_service_config()
        self.model_config_path = os.path.join(self.extract_model_config_path(), "config.json")
        self.model_config = self.load_model_config()
        self.model_name = self.extract_model_name()
        self.world_size = self.extract_world_size()
        self.llm_size = self.extract_llm_size()
        self.model_params = self.extract_model_params()
        self.sim_config_path = os.path.join(Path(__file__).resolve().parent.parent, 'config/config.json')
        self.hbm_size = self.extract_hbm_size()
        self.bytes_per_element = self.extract_bytes_per_element()

        self.tp = None
        self.dp = None

        self.dp = self.extract_dp()
        self.tp = self.extract_tp()
        
        self.model_params.update({
            "model_name": self.model_name,
            "llm_size": self.llm_size
        })

        self.hardware_params = {
            "world_size": self.world_size,
            "hbm_size": self.hbm_size
        }

        self.model_params.update({
            "tp": self.tp,
            "dp": self.dp,
            "bytes_per_element": self.bytes_per_element
        })

    @staticmethod
    def open_config_file(file_path):

        is_valid, error_msg = PathCheck.check_path_full(file_path)
        if is_valid:
            # get absolute PATH of 'vim'
            vim_path = shutil.which('vim')
            if vim_path is None:
                raise FileNotFoundError("vim not found in PATH!")
                
            subprocess.run([vim_path, file_path], check=True)
        else:
            raise FileNotFoundError(f'Path validation failed: {error_msg}')

    @staticmethod
    def extract_hbm_size():
        try:
            # get absolute PATH of 'npu-smi'
            npu_smi_path = shutil.which('npu-smi')
            if npu_smi_path is None:
                raise FileNotFoundError("'npu-smi' not found in PATH!")

            result = subprocess.run(
                [npu_smi_path, 'info'],
                capture_output=True,
                text=True,
                timeout=10
                )

            if result.returncode != 0:
                raise RuntimeError(f"Command failed: {result.stderr}")

            output = result.stdout

        except (OSError, RuntimeError) as e:
            raise RuntimeError("The command 'npu-smi info' failed, please set hardware config manually.") from e

        pattern = r'(\d+)\s*/\s*(\d+)'
        matches = re.findall(pattern, output)
        if len(matches) > 2 and len(matches[2]) > 1:
            hbm_size_str = matches[2][1]
            if hbm_size_str.startswith('3'):
                return HBM_32G
            elif hbm_size_str.startswith('6'):
                return HBM_64G
        hbm_size_input = input("Cannot extract hbm_size size from 'npu-smi info' command, please set manually: ")
        hbm_size = input_validate(hbm_size_input)
        return hbm_size

    def extract_bytes_per_element(self) -> int:
        """Extracts the number of bytes per element based on quantization configuration.
            
        Returns:
            int: 1 for 8-bit quantization (w8a8), 2 otherwise (default).
        """
        quantization_config = str(self.model_config.get('quantize', ''))

        bytes_per_element = 1 if '8' in quantization_config else 2

        return bytes_per_element

    def extract_version(self, keyword=VERSION_KEY):
        with PathCheck.safe_open(self.version_info_path, 'r', encoding='utf-8') as file:
            for line in file:
                if keyword in line:
                    version = line.split(':')[-1].strip()
                    return version
        return None

    def extract_world_size(self) -> int:
        try:
            # Get world_size from nested config structure
            world_size = (self.service_config.get("BackendConfig", {})
                        .get("ModelDeployConfig", {})
                        .get("ModelConfig", [{}])[0]
                        .get("worldSize"))
            
            if world_size is None:
                raise KeyError("worldSize not found in config")
            
            # Prompt user to confirm/modify the value
            user_input = input(
                    f"The current WorldSize in config.json is {world_size}. "
                    f"Please enter the actual WorldSize (or press Enter to use current value): "
                    )
            
            # Use user input if provided, else use config value
            if user_input.strip():
                try:
                    return int(user_input)
                except ValueError as e:
                    raise ValueError("Invalid input. Please enter an integer value for WorldSize.") from e
            
            return int(world_size)

        except (KeyError, IndexError) as e:
            # Re-raise with more descriptive message if config parsing fails
            raise KeyError("Failed to extract worldSize from config.json") from e

    def extract_model_config_path(self):
        try:
            model_weight_path = self.service_config.get("BackendConfig", {}). \
                get("ModelDeployConfig", {}). \
                get("ModelConfig", [{}])[0]. \
                get("modelWeightPath")

            if model_weight_path is None:
                raise KeyError("model_weight_path not found in config")

            return model_weight_path
        except (KeyError, IndexError) as e:
            raise KeyError("Failed to extract modelWeightPath from config.json") from e

    def extract_model_name(self):
        try:
            model_name = self.service_config.get("BackendConfig", {}). \
                get("ModelDeployConfig", {}). \
                get("ModelConfig", [{}])[0]. \
                get("modelName")

            if model_name is None:
                raise KeyError("model_name not found in config")

            return model_name

        except (KeyError, IndexError) as e:
            raise KeyError("Failed to extract modelName from config.json") from e

    def extract_model_params(self):
        def get_first_matching_value(possible_keys, param_name):
            for key in possible_keys:
                if key in self.model_config:
                    return self.model_config[key]
            raise KeyError(f"None of the possible keys for '{param_name}' were found in model_config.")

        hidden_size = get_first_matching_value(self.possible_hidden_size_keys, "hidden_size")
        num_hidden_layers = get_first_matching_value(self.possible_num_hidden_layers_keys, "num_hidden_layers")
        num_attention_heads = get_first_matching_value(self.possible_num_attention_heads_keys, "num_attention_heads")
        num_key_value_heads = get_first_matching_value(self.possible_num_key_value_heads_keys, "num_key_value_heads")

        return {
            "hidden_size": hidden_size,
            "num_hidden_layers": num_hidden_layers,
            "num_attention_heads": num_attention_heads,
            "num_key_value_heads": num_key_value_heads
        }

    def extract_llm_size(self):
        pattern = r'(?i).*?(\d+)b(?:$|-)'
        match = re.search(pattern, self.model_name)
        if match:
            llm_size = int(float(match.group(1)))
        else:
            llm_size = input_validate(
                input(
                    "Cannot find LLM size in model_name, please input the size(eg. If DeepSeek_R1_671B, input 671):"
                )
            )
        return llm_size

    def extract_tp(self):
        try:
            tp = self.service_config.get("BackendConfig", {}). \
                get("ModelDeployConfig", {}). \
                get("ModelConfig", [{}])[0]. \
                get("tp", 1)
            if self.dp == 1:
                tp = self.world_size
            return tp
        except (KeyError, IndexError) as e:
            raise KeyError("Failed to extract tp from config.json") from e

    def extract_dp(self):
        try:
            dp = self.service_config.get("BackendConfig", {}). \
                get("ModelDeployConfig", {}). \
                get("ModelConfig", [{}])[0]. \
                get("dp", 1)
            return dp
        except (KeyError, IndexError) as e:
            raise KeyError("Failed to extract dp from config.json") from e

    def get_config_file_path(self):
        self.config_file_path = os.path.join(
            os.getenv(
                "MIES_INSTALL_PATH", "/usr/local/Ascend/mindie/latest/mindie-service"
            ), "conf/config.json"
        )

    def get_version_info_path(self):
        self.version_info_path = os.path.abspath(
            os.path.join(
                os.getenv(
                    "MIES_INSTALL_PATH", "/usr/local/Ascend/mindie/latest/mindie-service"
                ), "../version.info"
            )
        )

    def updata_config(self, config_path):
        with PathCheck.safe_open(config_path, 'r', encoding='utf-8') as f_in:
            config = json.load(f_in)
            config["hardware_model_config"].update(self.hardware_params)
            config["hardware_model_config"].update(self.model_params)

            with PathCheck.safe_open(config_path, 'w', encoding='utf-8') as f_out:
                json.dump(config, f_out, indent=4)

    def load_model_config(self):
        if not os.path.exists(self.model_config_path):
            raise FileNotFoundError(f"File {self.model_config_path} not found")
        try:
            with PathCheck.safe_open(self.model_config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"File {self.model_config_path} has invalid format, cannot be parsed") from e

    def load_service_config(self):
        if not os.path.exists(self.config_file_path):
            raise FileNotFoundError(f"File {self.config_file_path} not found")
        try:
            with PathCheck.safe_open(self.config_file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"File {self.config_file_path} has invalid format, cannot be parsed") from e