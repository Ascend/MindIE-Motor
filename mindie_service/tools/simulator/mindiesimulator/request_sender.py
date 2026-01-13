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

from abc import ABC, abstractmethod
import numpy as np


class RequestStrategy(ABC):
    @abstractmethod
    def set_next_request_in_time(self, previous_time):
        pass


class UniformSender(RequestStrategy):
    def __init__(self, request_rate=1, random_seed=1):
        self.request_rate = request_rate
        self.random_seed = random_seed
        np.random.seed(self.random_seed)

    def set_random_seed(self, random_seed):
        self.random_seed = random_seed
        np.random.seed(self.random_seed)

    def set_next_request_in_time(self, previous_time=0):
        return previous_time + np.random.uniform(0, 1 / self.request_rate * 2)


class NonUniformSender(RequestStrategy):
    def __init__(self, request_rate=1):
        self.request_rate = request_rate

    def set_next_request_in_time(self, previous_time=0):
        return previous_time + 1 / self.request_rate