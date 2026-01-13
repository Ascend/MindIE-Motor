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
import argparse
from copy import deepcopy
from enum import Enum

from mindiesimulator.common.config_loader import ConfigLoader
from mindiesimulator.common.path_check import PathCheck

TYPE = "type"
MIN = "min"
MAX = "max"
MIN_STEP = "min_step"
DP_SIZE = "dp"
TP_SIZE = "tp"


class ConfigManager:
    # Version file path and key
    VERSION_INFO_PATH = os.path.abspath(
        os.path.join(
            os.getenv("MIES_INSTALL_PATH", "/usr/local/Ascend/mindie/latest/mindie-service"),
            "../version.info"
        )
    )
    VERSION_KEY = "mindie-service"

    # Enum definitions for strategies and run types
    class RunType(str, Enum):
        PARAM_TUNING = "param_tuning"
        SINGLE_SIM = "single_sim"
        LATENCY_MODEL_FIT = "latency_fit"
        CONFIG = "config"

    class TuningMode(str, Enum):
        GRID = "grid"
        PSO = "pso"

    def __init__(self):
        self.args = None

    @staticmethod
    def get_folder_label(model, version, hbm_size, tp, dp):
        return f"{model}__{version}__{hbm_size}G__TP{tp}__DP{dp}"

    @staticmethod
    def flatten_json(nested_json: dict, parent_key: str = '', separator: str = '.') -> dict:
        """Flatten nested JSON dictionaries."""
        flat = {}
        for key, value in nested_json.items():
            new_key = f"{parent_key}{separator}{key}" if parent_key else key
            if isinstance(value, dict):
                flat.update(ConfigManager.flatten_json(value, new_key, separator))
            else:
                flat[new_key] = value
        return flat

    @staticmethod
    def validate_model_name(name: str) -> bool:
        if not (1 <= len(name) <= 30):
            return False

        if not name[0].isalpha():
            return False

        for symbol in name[1:]:
            if not (symbol.isalnum() or symbol in ("_", "-")):
                return False

        return True

    @staticmethod
    def validate_file_path(path: str, file_type: str) -> str:
        if not os.path.exists(path):
            raise argparse.ArgumentTypeError(f"{file_type} file does not exist: {path}")
        if file_type == "JSON":
            if not path.lower().endswith('.json'):
                raise argparse.ArgumentTypeError(f"{file_type} file must be a .json file")
            try:
                with PathCheck.safe_open(path, 'r') as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                raise argparse.ArgumentTypeError(f"Invalid JSON file: {path}: {e}")
        return path

    @staticmethod
    def validate_directory(path: str) -> str:
        """Ensure that the directory exists (create if necessary)."""
        try:
            os.makedirs(path, exist_ok=True, mode=0o750)
            return path
        except Exception as e:
            raise argparse.ArgumentTypeError(f"Cannot create directory {path}: {e}")

    @staticmethod
    def save_hardware_model_config(path: str, config: dict) -> None:
        hardware_model_config = {
            'hbm_size': config.get('hbm_size'),
            'world_size': config.get('world_size'),
            'model_name': config.get('model_name'),
            'llm_size': config.get('llm_size'),
            'hidden_size': config.get('hidden_size'),
            'num_hidden_layers': config.get('num_hidden_layers'),
            'num_attention_heads': config.get('num_attention_heads'),
            'num_key_value_heads': config.get('num_key_value_heads')
        }
        with PathCheck.safe_open(path, 'w', encoding='utf-8') as f:
            json.dump(hardware_model_config, f, indent=4)

    @staticmethod
    def load_hardware_model_config(path: str) -> dict:
        with PathCheck.safe_open(path, 'r', encoding='utf-8') as f:
            hardware_model_config = json.load(f)
        return hardware_model_config

    @classmethod
    def validate_config(cls, config_path: str) -> dict:
        default_config = {
            "hardware_model_config.enable_auto_config": True,
            "hardware_model_config.world_size": 2,
            "hardware_model_config.hbm_size": 64,
            "hardware_model_config.tp_size": 1,
            "hardware_model_config.dp_size": 1,
            "hardware_model_config.model_name": "llama3-8b",
            "hardware_model_config.llm_size": 8,
            "hardware_model_config.bytest_per_element": 2,
            "hardware_model_config.hidden_size": 5120,
            "hardware_model_config.num_hidden_layers": 80,
            "hardware_model_config.num_attention_heads": 64,
            "hardware_model_config.num_key_value_heads": 8,
            "single_sim.npu_mem_size": -1,
            "single_sim.block_size": 128,
            "single_sim.concurrency": 1000,
            "single_sim.request_rate": 20,
            "single_sim.max_seq_len": 2560,
            "single_sim.max_input_len": 2048,
            "single_sim.max_prefill_batch_size": 50,
            "single_sim.max_prefill_tokens": 8192,
            "single_sim.prefill_time_ms_per_request": 150,
            "single_sim.decode_time_ms_per_request": 50,
            "single_sim.max_batch_size": 200,
            "single_sim.support_select_batch": True,
            "single_sim.recompute_ratio": 0.5,
            "param_tuning.limits.avg_prefill_time_thld": 1000,
            "param_tuning.limits.avg_decode_time_thld": 50,
            "param_tuning.limits.SLOP90_prefill_time_thld": 1000,
            "param_tuning.limits.SLOP90_decode_time_thld": 50,
            "param_tuning.fixed_params.npu_mem_size": -1,
            "param_tuning.fixed_params.block_size": 128,
            "param_tuning.fixed_params.max_seq_len": 2560,
            "param_tuning.fixed_params.max_input_len": 2048,
            "param_tuning.fixed_params.max_prefill_tokens": 8192,
            "param_tuning.fixed_params.decode_time_ms_per_request": 50,
            "param_tuning.fixed_params.support_select_batch": True,
            "param_tuning.fixed_params.recompute_ratio": 0.5,
            "param_tuning.param_ranges.concurrency_range": [10, 1000, 10],
            "param_tuning.param_ranges.request_rate_range": [0.5, 30, 0.5],
            "param_tuning.param_ranges.max_prefill_batch_size_range": [50, 100, 5],
            "param_tuning.param_ranges.prefill_time_ms_per_request_range": [50, 1000, 2],
            "param_tuning.param_ranges.max_batch_size_range": [10, 100, 10]
        }
        # Scalar parameter constraints: type and [min, max]
        scalar_constraints = {
            'hardware_model_config.world_size': {TYPE: int, MIN: 1, MAX: 256},
            'hardware_model_config.tp_size': {TYPE: int, MIN: 1, MAX: 64},
            'hardware_model_config.dp_size': {TYPE: int, MIN: 1, MAX: 64},
            'hardware_model_config.llm_size': {TYPE: int, MIN: 1, MAX: 5000},
            'hardware_model_config.bytest_per_element': {TYPE: int, MIN: 1, MAX: 4},
            'hardware_model_config.hidden_size': {TYPE: int, MIN: 1, MAX: 100000},
            'hardware_model_config.num_hidden_layers': {TYPE: int, MIN: 1, MAX: 1000},
            'hardware_model_config.num_attention_heads': {TYPE: int, MIN: 1, MAX: 1000},
            'hardware_model_config.num_key_value_heads': {TYPE: int, MIN: 1, MAX: 1000},
            'single_sim.npu_mem_size': {TYPE: int, MIN: -1, MAX: 64},
            'single_sim.block_size': {TYPE: int, MIN: 1, MAX: 1000},
            'single_sim.concurrency': {TYPE: int, MIN: 1, MAX: 4096},
            'single_sim.request_rate': {TYPE: (int, float), MIN: 0, MAX: 1000.0},
            'single_sim.max_seq_len': {TYPE: int, MIN: 1, MAX: 100000},
            'single_sim.max_input_len': {TYPE: int, MIN: 1, MAX: 100000},
            'single_sim.max_prefill_batch_size': {TYPE: int, MIN: 1, MAX: 1000},
            'single_sim.max_prefill_tokens': {TYPE: int, MIN: 1, MAX: 1000000},
            'single_sim.prefill_time_ms_per_request': {TYPE: int, MIN: 1, MAX: 1000},
            'single_sim.decode_time_ms_per_request': {TYPE: int, MIN: 1, MAX: 1000},
            'single_sim.max_batch_size': {TYPE: int, MIN: 1, MAX: 1000},
            'single_sim.recompute_ratio': {TYPE: (int, float), MIN: 0.1, MAX: 1},
            'param_tuning.fixed_params.npu_mem_size': {TYPE: int, MIN: -1, MAX: 64},
            'param_tuning.fixed_params.block_size': {TYPE: int, MIN: 1, MAX: 1000},
            'param_tuning.fixed_params.max_seq_len': {TYPE: int, MIN: 1, MAX: 100000},
            'param_tuning.fixed_params.max_input_len': {TYPE: int, MIN: 1, MAX: 100000},
            'param_tuning.fixed_params.max_prefill_tokens': {TYPE: int, MIN: 1, MAX: 1000000},
            'param_tuning.fixed_params.decode_time_ms_per_request': {TYPE: int, MIN: 1, MAX: 1000},
            'param_tuning.fixed_params.recompute_ratio': {TYPE: (int, float), MIN: 0.1, MAX: 1},
            'param_tuning.limits.avg_prefill_time_thld': {TYPE: int, MIN: -1, MAX: 1000000},
            'param_tuning.limits.avg_decode_time_thld': {TYPE: int, MIN: -1, MAX: 1000000},
            'param_tuning.limits.SLOP90_prefill_time_thld': {TYPE: int, MIN: -1, MAX: 1000000},
            'param_tuning.limits.SLOP90_decode_time_thld': {TYPE: int, MIN: -1, MAX: 1000000}
        }
        # Range parameter constraints: type, lower bound, upper bound and minimal step size
        range_constraints = {
            'param_tuning.param_ranges.concurrency_range': {
                TYPE: int, MIN: 1, MAX: 1000, MIN_STEP: 1
            },
            'param_tuning.param_ranges.request_rate_range': {
                TYPE: (int, float), MIN: 0.01, MAX: 1000.0, MIN_STEP: 0.1
            },
            'param_tuning.param_ranges.max_prefill_batch_size_range': {
                TYPE: int, MIN: 1, MAX: 1000, MIN_STEP: 1
            },
            'param_tuning.param_ranges.prefill_time_ms_per_request_range': {
                TYPE: int, MIN: 1, MAX: 1000, MIN_STEP: 1
            },
            'param_tuning.param_ranges.max_batch_size_range': {
                TYPE: int, MIN: 1, MAX: 1000, MIN_STEP: 1
            }
        }
        # Enum constraints: allowed values
        enum_constraints = {
            "hardware_model_config.enable_auto_config": [True, False],
            'hardware_model_config.hbm_size': [32, 64],
            'single_sim.support_select_batch': [True, False],
            'param_tuning.fixed_params.support_select_batch': [True, False]
        }

        try:
            with PathCheck.safe_open(config_path, 'r') as f:
                params = json.load(f)
            params = cls.flatten_json(params)
            merged = deepcopy(default_config)
            merged.update(params)
            params = merged
        except Exception as e:
            raise ValueError(f"Failed to load configuration file: {e}") from e

        # Check for required parameters
        required_keys = set(scalar_constraints) | set(range_constraints) | set(enum_constraints)
        missing = required_keys - set(params.keys())
        if missing:
            raise ValueError(f"Missing required parameters: {missing}")

        # Validate enum parameters
        for key, valid in enum_constraints.items():
            if params.get(key) not in valid:
                raise ValueError(f"Invalid value for {key}. Expected one of {valid}, got {params.get(key)}")

        if not cls.validate_model_name(params.get("hardware_model_config.model_name")):
            raise ValueError(f"Invalid hardware model name: {params.get('hardware_model_config.model_name')}, "
                             f"it should be start with english alphabet and not beyond 30 letters.")

        # Validate scalar parameters
        for key, cons in scalar_constraints.items():
            value = params.get(key)
            if not isinstance(value, cons[TYPE]):
                raise ValueError(f"Invalid type for {key}. Expected {cons['type']}, got {type(value)}")
            if not (cons[MIN] <= value <= cons[MAX]):
                raise ValueError(f"{key} must be between {cons['min']} and {cons['max']}, got {value}")

        # Validate range parameters
        for key, cons in range_constraints.items():
            value = params.get(key)
            if not (isinstance(value, list) and len(value) == 3):
                raise ValueError(f"{key} must be a list with 3 elements [lower_bound, upper_bound, step_size]")
            lower, upper, step = value
            if not all(isinstance(x, cons[TYPE]) for x in value):
                raise ValueError(f"All elements in {key} must be of type {cons['type']}")
            if not (cons[MIN] <= lower <= cons[MAX]):
                raise ValueError(f"Lower bound in {key} must be between {cons['min']} and {cons['max']}")
            if not (cons[MIN] <= upper <= cons[MAX]):
                raise ValueError(f"Upper bound in {key} must be between {cons['min']} and {cons['max']}")
            if lower > upper:
                raise ValueError(f"In {key}, lower bound must not exceed upper bound")
            if step <= 0 or step < cons[MIN_STEP]:
                raise ValueError(f"In {key}, step must be positive and at least {cons['min_step']}")
        return params

    @classmethod
    def extract_version(cls, file_path: str = None, keyword: str = VERSION_KEY) -> str:
        file_path = file_path or cls.VERSION_INFO_PATH
        try:
            with PathCheck.safe_open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if keyword in line:
                        return line.split(':')[-1].strip() or "UNKNOWN"
        except (FileNotFoundError, IOError):
            # Return "UNKNOWN" to maintain consistent label format in case of error.
            pass
        return "UNKNOWN"

    def parse_args(self) -> argparse.Namespace:
        parser = argparse.ArgumentParser(description='Simulator Configuration Parser')
        parser.add_argument(
            '--RunType',
            type=str,
            choices=[e.value for e in self.RunType],
            default=self.RunType.PARAM_TUNING.value,
            help=f'Execution type (default: {self.RunType.PARAM_TUNING.value})'
        )
        parser.add_argument(
            '--TuningMode',
            type=str,
            choices=[e.value for e in self.TuningMode],
            default=self.TuningMode.PSO.value,
            help=f'Parameter tuning strategy (default: {self.TuningMode.PSO.value})'
        )
        self.args = parser.parse_args()
        return self.args


def get_single_sim_params(config_params):
    config = {
        'auto_config': config_params.get('hardware_model_config.enable_auto_config'),
        'hbm_size': config_params.get('hardware_model_config.hbm_size'),
        'world_size': config_params.get('hardware_model_config.world_size'),
        DP_SIZE: config_params.get('hardware_model_config.dp_size'),
        TP_SIZE: (config_params.get('hardware_model_config.world_size') //
            config_params.get('hardware_model_config.dp_size')),
        'model_name': config_params.get('hardware_model_config.model_name'),
        'llm_size': config_params.get('hardware_model_config.llm_size'),
        'bytes_per_element': config_params.get('hardware_model_config.bytes_per_element'),
        'hidden_size': config_params.get('hardware_model_config.hidden_size'),
        'num_hidden_layers': config_params.get('hardware_model_config.num_hidden_layers'),
        'num_attention_heads': config_params.get('hardware_model_config.num_attention_heads'),
        'num_key_value_heads': config_params.get('hardware_model_config.num_key_value_heads'),
        'npu_mem_size': config_params.get('single_sim.npu_mem_size'),
        'block_size': config_params.get('single_sim.block_size'),
        'concurrency': config_params.get('single_sim.concurrency'),
        'request_rate': config_params.get('single_sim.request_rate'),
        'max_seq_len': config_params.get('single_sim.max_seq_len'),
        'max_input_len': config_params.get('single_sim.max_input_len'),
        'max_prefill_batch_size': config_params.get('single_sim.max_prefill_batch_size'),
        'max_prefill_tokens': config_params.get('single_sim.max_prefill_tokens'),
        'prefill_time_ms_per_request': config_params.get('single_sim.prefill_time_ms_per_request'),
        'decode_time_ms_per_request': config_params.get('single_sim.decode_time_ms_per_request'),
        'max_batch_size': config_params.get('single_sim.max_batch_size'),
        'support_select_batch': config_params.get('single_sim.support_select_batch'),
        'recompute_ratio': config_params.get('single_sim.recompute_ratio'),
    }

    if config['auto_config']:
        config_loader = ConfigLoader()
        config.update(config_loader.hardware_params)
        config.update(config_loader.model_params)

    config['hardware_model_info'] = ConfigManager.get_folder_label(
        config['model_name'],
        ConfigManager.extract_version(),
        config['hbm_size'],
        config[TP_SIZE],
        config[DP_SIZE]
    )
    return config


def get_param_tuning_params(config_params):
    fixed_params = {
        'auto_config': config_params.get('hardware_model_config.enable_auto_config'),
        'hbm_size': config_params.get('hardware_model_config.hbm_size'),
        'world_size': config_params.get('hardware_model_config.world_size'),
        DP_SIZE: config_params.get('hardware_model_config.dp_size'),
        TP_SIZE: (config_params.get('hardware_model_config.world_size') //
            config_params.get('hardware_model_config.dp_size')),
        'model_name': config_params.get('hardware_model_config.model_name'),
        'llm_size': config_params.get('hardware_model_config.llm_size'),
        'hidden_size': config_params.get('hardware_model_config.hidden_size'),
        'bytes_per_element': config_params.get('hardware_model_config.bytes_per_element'),
        'num_hidden_layers': config_params.get('hardware_model_config.num_hidden_layers'),
        'num_attention_heads': config_params.get('hardware_model_config.num_attention_heads'),
        'num_key_value_heads': config_params.get('hardware_model_config.num_key_value_heads'),
        'npu_mem_size': config_params.get('param_tuning.fixed_params.npu_mem_size'),
        'block_size': config_params.get('param_tuning.fixed_params.block_size'),
        'max_seq_len': config_params.get('param_tuning.fixed_params.max_seq_len'),
        'max_input_len': config_params.get('param_tuning.fixed_params.max_input_len'),
        'max_prefill_tokens': config_params.get('param_tuning.fixed_params.max_prefill_tokens'),
        'decode_time_ms_per_request': config_params.get('param_tuning.fixed_params.decode_time_ms_per_request'),
        'support_select_batch': config_params.get('param_tuning.fixed_params.support_select_batch'),
        'recompute_ratio': config_params.get('param_tuning.fixed_params.recompute_ratio'),
    }

    params_range = {
        'concurrency': config_params.get('param_tuning.param_ranges.concurrency_range'),
        'request_rate': config_params.get('param_tuning.param_ranges.request_rate_range'),
        'max_prefill_batch_size': config_params.get('param_tuning.param_ranges.max_prefill_batch_size_range'),
        'prefill_time_ms_per_request': config_params.get('param_tuning.param_ranges.prefill_time_ms_per_request_range'),
        'max_batch_size': config_params.get('param_tuning.param_ranges.max_batch_size_range')
    }

    limits = {
        'avg_prefill_time_thld': config_params.get('param_tuning.limits.avg_prefill_time_thld'),
        'avg_decode_time_thld': config_params.get('param_tuning.limits.avg_decode_time_thld'),
        'SLOP90_prefill_time_thld': config_params.get('param_tuning.limits.SLOP90_prefill_time_thld'),
        'SLOP90_decode_time_thld': config_params.get('param_tuning.limits.SLOP90_decode_time_thld')
    }

    if fixed_params['auto_config']:
        config_loader = ConfigLoader()
        fixed_params.update(config_loader.hardware_params)
        fixed_params.update(config_loader.model_params)

    fixed_params['hardware_model_info'] = ConfigManager.get_folder_label(fixed_params['model_name'],
                                                                         ConfigManager.extract_version(),
                                                                         fixed_params['hbm_size'],
                                                                         fixed_params['world_size'],
                                                                         fixed_params[DP_SIZE])
    return fixed_params, params_range, limits





