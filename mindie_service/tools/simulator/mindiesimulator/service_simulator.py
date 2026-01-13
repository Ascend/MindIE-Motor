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

import json
import os

from mindiesimulator.request_sender import UniformSender, NonUniformSender
from mindiesimulator.latency_modeling import LatencyModeling, PolyFitter
from mindiesimulator.inference_engine import InferenceSim
from mindiesimulator.scheduler import Scheduler
from mindiesimulator.common.path_check import PathCheck


class ServiceSimulator:
    def __init__(self, config):
        if config is None:
            raise ValueError("Config parameter 'config' is required and cannot be None")
        self._config = config
        self.req_rate = self._config.get('request_rate')
        self.scenario = self._config.get('scenario')
        self.hardware_model_info = self._config.get('hardware_model_info')
        self.dp = self._config.get('dp')

        self.request_sender = None
        self.set_request_sender()
        self.latency_model = None
        self.set_latency_model()
        self.inference_engine = None
        self.set_inference_engine()

        self.input_output_len = None
        self.get_input_output_len(self.scenario)
        self.scheduler = None
        self.set_scheduler()

    def set_request_sender(self):
        self.request_sender = UniformSender(request_rate=self.req_rate)

    def set_latency_model(self):
        poly_fitter = PolyFitter(self.scenario, dp=self.dp)
        self.latency_model = LatencyModeling(self.scenario, poly_fitter)

    def set_inference_engine(self):
        self.latency_model.load_model()
        self.inference_engine = InferenceSim(self._config, self.latency_model)

    def set_scheduler(self):
        self.scheduler = Scheduler(self._config, self.input_output_len, self.request_sender, self.inference_engine)

    def get_input_output_len(self, file_path):
        
        with PathCheck.safe_open(os.path.join(file_path, 'input_output_len.json'), 'r') as file:
            self.input_output_len = json.load(file)

    def run_schedule(self):
        self.scheduler.run_schedule()

    def get_sim_results(self):
        return self.scheduler.sim_result