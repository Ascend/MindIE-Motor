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
import glob
import shutil
from pathlib import Path
from .utils import Logger
from .path_check import PathCheck

INSTANCE_DIR = 'instance'
SCENARIO_DIR = 'scenario'
CUSTOM_DIR = 'custom'
SIMULATOR_DIR = 'simulator'


class ScenarioLoader:
    def __init__(self, folder_name):
        self.target_folder_name = folder_name
        self.current_path = Path(__file__).resolve().parent.parent
        self.instance_folder_path = Path.cwd() / INSTANCE_DIR
        self.custom_folder_path = self.current_path / SCENARIO_DIR / CUSTOM_DIR
        self.target_folder_path = self.current_path / SCENARIO_DIR / self.target_folder_name
        self.data_folder_path = self.current_path / SCENARIO_DIR
        self.available_scenarios = None
        self.merged_data = None
        self.batch_info_path = None
        self.req_to_data_map_path = None
        self.results_per_request_path = None
        self.logger = Logger.get_logger()

    @staticmethod
    def read_json_file(file_path):
        with PathCheck.safe_open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    @staticmethod
    def read_json_lines_file(file_path):
        with PathCheck.safe_open(file_path, 'r') as f:
            return [json.loads(line) for line in f]

    @staticmethod
    def get_latest_file(folder_path, prefix):
        pattern = r'results_per_request_(\d+)\.json'
        files = glob.glob(os.path.join(folder_path, f"{prefix}*.json"))
        if not files:
            return None
        files.sort(key=lambda f: int(re.search(pattern, f).group(1)), reverse=True)
        return files[0]

    @staticmethod
    def prompt_user(message):
        return input(message).strip().lower()

    def merge_files(self):
        batch_info = self.read_json_lines_file(self.batch_info_path)
        results_per_request = self.read_json_file(self.results_per_request_path)
        req_to_data_map = self.read_json_file(self.req_to_data_map_path)
        merged = {
            'batch_info': batch_info,
            'results_per_requst': results_per_request,
            'req_data_map': req_to_data_map
        }
        return merged

    def save_latency_data(self):
        target_folder = self.target_folder_path
        if os.path.exists(target_folder):
            latency_file = os.path.join(target_folder, 'latency_data.json')
            if os.path.isfile(latency_file):
                select_overwrite = self.prompt_user(
                    f"Folder '{target_folder}' already exists and includes 'latency_data.json',"
                    f" do you want to overwrite? (y/n)"
                )
                if select_overwrite.lower() in ['y', 'yes']:
                    shutil.rmtree(target_folder)
                    os.makedirs(target_folder, mode=0o750)
                else:
                    target_folder = self.create_new_folder_with_suffix(self.target_folder_name)
                    self.target_folder_path = target_folder
            else:
                pass
        else:
            os.makedirs(target_folder, mode=0o750)

        self.merged_data = self.merge_files()
        with PathCheck.safe_open(os.path.join(target_folder, 'latency_data.json'), 'w', encoding='utf-8') as f:
            json.dump(self.merged_data, f, indent=4)

    def run(self):
        select_auto_load = self.prompt_user("Do you want to auto-load from environment? (y/n)")
        if select_auto_load.lower() in ['y', 'yes']:
            self.handle_auto_mode()
        else:
            select_custom_load = self.prompt_user("Do you want to load from custom folder? (y/n)")
            if select_custom_load.lower() in ['y', 'yes']:
                self.handle_custom_mode()
            else:
                self.logger.info("No need to load latency data, please choose scenario directly.")

    def create_new_folder_with_suffix(self, base_name):
        pattern = re.compile(rf"^{re.escape(base_name)}_(\d+)$")
        existing_folders = [entry.name for entry in os.scandir(self.data_folder_path) if entry.is_dir()]
        suffixes = []
        for folder in existing_folders:
            match = pattern.match(folder)
            if match:
                suffixes.append(int(match.group(1)))
        if suffixes:
            new_suffix = max(suffixes) + 1
        else:
            new_suffix = 1
        while True:
            new_folder_name = f"{base_name}_{new_suffix}"
            new_folder_path = os.path.join(self.data_folder_path, new_folder_name)
            os.makedirs(new_folder_path, mode=0o750)
            return new_folder_path

    def handle_auto_mode(self):
        self.batch_info_path = os.getenv("MINDIE_LLM_BENCHMARK_FILEPATH",
                                         os.path.join(os.getenv("MINDIE_LLM_HOME_PATH"), "logs/benchmark.jsonl"))
        self.req_to_data_map_path = self.instance_folder_path / 'req_to_data_map.json'
        self.results_per_request_path = self.get_latest_file(self.instance_folder_path, 'results_per_request')
        self.save_latency_data()


    def handle_custom_mode(self):
        if not os.path.exists(self.custom_folder_path):
            raise FileNotFoundError(f"Folder 'custom' not found in simulator scenarios.")

        self.batch_info_path = os.path.join(self.custom_folder_path, 'benchmark.jsonl')
        self.req_to_data_map_path = os.path.join(self.custom_folder_path, 'req_to_data_map.json')

        results_per_request_file = glob.glob(os.path.join(self.custom_folder_path, "results_per_request*.json"))
        if not results_per_request_file:
            raise FileNotFoundError(f"The results_per_request file not found in custom folder.")
        if len(results_per_request_file) > 1:
            raise FileNotFoundError(f"More than one results_per_request file in custom folder.")
        self.results_per_request_path = results_per_request_file[0]
        self.save_latency_data()

    def list_scenarios(self):
        scenarios = [
            entry.name
            for entry in os.scandir(self.data_folder_path)
            if entry.is_dir() and entry.name != "custom"
        ]
        self.available_scenarios = scenarios
        if self.available_scenarios == []:
            raise FileNotFoundError("No scenario found in scenario folder.")


    def select_scenario(self):
        self.logger.info("The available scenarios are listed below:")
        for idx, folder in enumerate(self.available_scenarios, 1):
            self.logger.info(f"{idx}. {folder}")
        while True:
            selection = int(input("Please choose the number: "))
            if 1 <= selection <= len(self.available_scenarios):
                selected_scenario = self.available_scenarios[selection - 1]
                self.logger.info(f"The scenario you selected: {selected_scenario}")
                return self.data_folder_path / selected_scenario
            else:
                self.logger.info(f"The Number you select: {selection} is invalid. Please try again.")
