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

import logging
from pathlib import Path
import os

from mindiesimulator.service_simulator import ServiceSimulator
from mindiesimulator.parameter_solver import ParameterSolver, PSOSearch, GridSearch
from mindiesimulator.latency_modeling import LatencyModeling, PolyFitter

from mindiesimulator.common.scenario_loader import ScenarioLoader
from mindiesimulator.common.config_manager import ConfigManager, get_single_sim_params, get_param_tuning_params
from mindiesimulator.common.utils import Logger
from mindiesimulator.common.config_loader import ConfigLoader

HARDWARE_MODEL_CONFIG = 'hardware_model_config.json'
AUTO_CONFIG = 'auto_config'
SCENARIO = 'scenario'
MODEL_NAME = 'model_name'
HBM_SIZE = 'hbm_size'
TP_SIZE = 'tp'
DP_SIZE = 'dp'


def main():
    config_manager = ConfigManager()
    command_args = config_manager.parse_args()
    config_path = os.path.join(Path(__file__).resolve().parent, 'config/config.json')
    save_path = os.path.join(Path(__file__).resolve().parent, 'result')
    scenario_path = os.path.join(Path(__file__).resolve().parent, SCENARIO)
    custom_path = os.path.join(Path(__file__).resolve().parent, 'scenario/custom')
    config_params = config_manager.validate_config(config_path)
    os.makedirs(scenario_path, exist_ok=True, mode=0o750)
    os.makedirs(custom_path, exist_ok=True, mode=0o750)

    if command_args.RunType == "config":
        try:
            ConfigLoader.open_config_file(config_path)
        except RuntimeError as e:
            raise RuntimeError("Open config file failed: %s", e) from e

    elif command_args.RunType == "latency_fit":
        Logger.init_logger(log_dir=save_path, exec_type="latency_fit", level=logging.INFO, save_to_file=True)
        logger = Logger.get_logger()
        logger.info("Start latency model fitting...")
        try:
            config = get_single_sim_params(config_params)
            folder_label = config_manager.get_folder_label(
                config.get(MODEL_NAME),
                config_manager.extract_version(),
                config.get(HBM_SIZE),
                config.get(TP_SIZE),
                config.get(DP_SIZE),
            )
            scenario_loader = ScenarioLoader(folder_label)
            scenario_loader.run()
            scenario_loader.list_scenarios()
            data_path = scenario_loader.select_scenario()
            if config[AUTO_CONFIG]:
                config_manager.save_hardware_model_config(
                    os.path.join(
                        data_path, HARDWARE_MODEL_CONFIG
                    ),
                    config
                )
            poly_fitter = PolyFitter(data_path)
            latency_model = LatencyModeling(data_path=data_path, fit_strategy=poly_fitter)
            latency_model.get_latency_data()
            latency_model.extract_input_output_len()
            latency_model.save_input_output_len()
            latency_model.parse_data()
            latency_model.get_frametime()
            latency_model.prefill_model_time_fit()
            latency_model.prefill_frame_time_fit()
            latency_model.decode_model_time_fit()
            latency_model.decode_frame_time_fit()
            latency_model.save_model()

            logger.info("Latency model fitting completed successfully")
        except RuntimeError as e:
            logging.error("Latency model fitting failed: %s", e)
            raise RuntimeError("Latency model fitting failed: %s", e) from e
            
    elif command_args.RunType == "single_sim":
        Logger.init_logger(log_dir=save_path, exec_type="single_sim", level=logging.INFO, save_to_file=True)
        logger = Logger.get_logger()
        logger.info("Start single simulation...")
        try:
            config = get_single_sim_params(config_params)
            folder_label = config_manager.get_folder_label(
                config.get(MODEL_NAME),
                config_manager.extract_version(),
                config.get(HBM_SIZE),
                config.get(TP_SIZE),
                config.get(DP_SIZE),
            )
            scenario_loader = ScenarioLoader(folder_label)
            scenario_loader.list_scenarios()
            selected_scenario = scenario_loader.select_scenario()
            config[SCENARIO] = selected_scenario
            if config[AUTO_CONFIG]:
                hardware_model_config = config_manager.load_hardware_model_config(
                    os.path.join(
                        selected_scenario, HARDWARE_MODEL_CONFIG
                    )
                )
                config.update(hardware_model_config)
            service_sim = ServiceSimulator(config)
            logger.info(f"Config info: {config}")
            service_sim.run_schedule()
            logger.info("Single simulation completed successfully")
        except RuntimeError as e:
            logging.error("Single simulation failed: %s", e)
            raise RuntimeError("Single simulation failed: %s", e) from e

    elif command_args.RunType == "param_tuning":
        Logger.init_logger(log_dir=save_path, exec_type="param_tuning", level=logging.INFO, save_to_file=True)
        logger = Logger.get_logger()
        logger.info("Start parameter tuning...")
        try:
            fixed_params, param_ranges, constraints = get_param_tuning_params(config_params)
            folder_label = config_manager.get_folder_label(
                fixed_params.get(MODEL_NAME),
                config_manager.extract_version(),
                fixed_params.get(HBM_SIZE),
                fixed_params.get(TP_SIZE),
                fixed_params.get(DP_SIZE),
            )
            scenario_loader = ScenarioLoader(folder_label)
            scenario_loader.list_scenarios()
            selected_scenario = scenario_loader.select_scenario()
            fixed_params[SCENARIO] = selected_scenario
            if fixed_params[AUTO_CONFIG]:
                hardware_model_config = config_manager.load_hardware_model_config(
                    os.path.join(
                        selected_scenario, HARDWARE_MODEL_CONFIG
                    )
                )
                fixed_params.update(hardware_model_config)
            logger.info("param_ranges", param_ranges)
            if command_args.TuningMode == config_manager.TuningMode.PSO.value:
                logger.info('Start PSO search...')
                param_tune_strategy = PSOSearch(50, 10)
            else:
                logger.info('Start Grid search...')
                param_tune_strategy = GridSearch()
            solver = ParameterSolver(fixed_params, param_tune_strategy)
            solver.set_ranges(param_ranges)
            solver.set_constraints(constraints)
            best_params = solver.run_solver()
            logger.info(f'best_params: {best_params}')
            logger.info("Parameter tuning completed successfully")
        except RuntimeError as e:
            logging.error("Parameter tuning failed: %s", e)
            raise RuntimeError("Parameter tuning failed: %s", e) from e

    else:
        logging.error("Unknown RunType: %s", command_args.RunType)
        raise RuntimeError("Unknown RunType: %s", command_args.RunType) from command_args.RunType


if __name__ == "__main__":
    main()