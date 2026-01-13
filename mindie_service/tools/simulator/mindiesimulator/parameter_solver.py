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

import copy
import random
from abc import ABC, abstractmethod
from itertools import product

import numpy as np

from mindiesimulator.service_simulator import ServiceSimulator
from mindiesimulator.common.utils import Logger


class OptimizationStrategy(ABC):
    @abstractmethod
    def set_parameter_ranges(self, ranges: dict) -> None:
        pass

    @abstractmethod
    def set_constraints(self, constraints: dict) -> None:
        pass

    @abstractmethod
    def has_next(self) -> bool:
        pass

    @abstractmethod
    def get_next_params(self) -> dict:
        pass

    @abstractmethod
    def update(self, params: dict, result: dict) -> None:
        pass

    @abstractmethod
    def get_best_params(self) -> dict:
        pass


class GridSearch(OptimizationStrategy):
    def __init__(self):
        self.params_list = []
        self.current_index = 0
        self.best_params = None
        self.best_result = float('-inf')
        self.best_result_latency = None
        self.constraints = {}
        self.performance_key = "performance"
        self.logger = Logger.get_logger()

    def set_parameter_ranges(self, ranges: dict) -> None:
        discrete_values = {}
        for param, (low, high, step) in ranges.items():
            if step <= 0:
                raise ValueError(f"Step value must be positive")
            values = []
            val = low
            while val <= high:
                values.append(val)
                val += step
            discrete_values[param] = values

        keys = list(discrete_values.keys())
        values_product = product(*(discrete_values.get(k) for k in keys))
        self.params_list = [dict(zip(keys, vals)) for vals in values_product]

    def set_constraints(self, constraints: dict) -> None:
        self.constraints = constraints

    def has_next(self) -> bool:
        return self.current_index < len(self.params_list)

    def get_next_params(self) -> dict:
        if not self.has_next():
            return {}
        params = self.params_list[self.current_index]
        return params

    def update(self, params: dict, result: dict) -> None:
        if not self._check_constraints(result):
            return

        current_perf = result.get(self.performance_key, float('-inf'))
        current_latency = {k: result[k] for k in result.keys() if k != self.performance_key}
        if current_perf > self.best_result:
            self.best_result = current_perf
            self.best_result_latency = current_latency
            self.best_params = params
            self.logger.info(f'Updata best params: {self.best_params}')

        self.logger.info(
            f"Current iteration {self.current_index+1}/{len(self.params_list)} result:\n"
            f"Current parameters: {self.params_list[self.current_index]}\n"
            f"Current latencies:\n"
            f"    avg_prefill_delay={current_latency['avg_prefill_time']},"
            f" p90_prefill_delay={current_latency['p90_prefill_time']},"
            f" avg_decode_delay={current_latency['avg_decode_time']},"
            f" p90_decode_delay={current_latency['p90_decode_time']}\n"

            f"Current best parameters: {self.best_params}, current best TPS: {self.best_result}\n"

            f"Current best parameters\' latencies:\n"
            f"    avg_prefill_delay={self.best_result_latency['avg_prefill_time']},"
            f" p90_prefill_delay={self.best_result_latency['p90_prefill_time']},"
            f" avg_decode_delay={self.best_result_latency['avg_decode_time']},"
            f" p90_decode_delay={self.best_result_latency['p90_decode_time']}"
        )
        self.current_index += 1

    def get_best_params(self) -> dict:
        self.logger.info(f'Best result: {self.best_result}')
        self.logger.info(
            f"Best parameters\' latencies:\n"
            f"    avg_prefill_delay={self.best_result_latency['avg_prefill_time']},"
            f" p90_prefill_delay={self.best_result_latency['p90_prefill_time']},"
            f" avg_decode_delay={self.best_result_latency['avg_decode_time']},"
            f" p90_decode_delay={self.best_result_latency['p90_decode_time']}"
        )
        return self.best_params if self.best_params is not None else {}

    def _check_constraints(self, result: dict) -> bool:
        for metric, limit in self.constraints.items():
            if metric in result and result[metric] > limit:
                return False
        return True


class PSOSearch(OptimizationStrategy):
    def __init__(self, n_particles, n_iterations):
        self.params_bounds = []
        self.current_index = 0
        self.best_params = None
        self.best_result = float('-inf')
        self.best_result_latency = None
        self.constraints = {}
        self.performance_key = "performance"
        self.tune_params = None

        self.particles = []
        self.velocities = []
        self.pbest_positions = []
        self.pbest_values = []
        self.max_initialization_attempts = 100

        self.gbest_position = None
        self.gbest_value = float('inf')
        self.gbest_thp = None
        self.gbest_params = None
        self.gbest_avg_prefill_delay = None
        self.gbest_p90_prefill_delay = None
        self.gbest_avg_decode_delay = None
        self.gbest_p90_decode_delay = None

        self.n_particles = n_particles
        self.n_iterations = n_iterations
        self.current_particle = 0
        self.current_iteration = -1

        self.penalty_coefficients = None

        self.penalty_coefficients = {
            'avg_prefill_delay': 1000,
            'avg_decode_delay': 1000,
            'p90_prefill_delay': 1000,
            'p90_decode_delay': 1000,
        }

        self.logger = Logger.get_logger()

    def init_particles(self):
        for i in range(self.n_particles):
            attempts = 0
            while attempts < self.max_initialization_attempts:
                position = [
                    value[0] + np.random.uniform(0, 0.1 * (value[1] - value[0]))
                    for value in self.params_bounds
                ]

                if position[2] > position[4]:
                    self.logger.info(
                        f"Initializing particle {i + 1} attempt {attempts + 1}: "
                        f"max_prefill_batch_size ({position[2]}) > max_batch_size ({position[4]}),"
                        f" skipping this particle."
                    )
                    attempts += 1
                    continue
                else:
                    self.particles.append(position)
                    velocity = [
                        np.random.uniform(-0.1 * (value[1] - value[0]), 0.1 * (value[1] - value[0]))
                        for value in self.params_bounds
                    ]
                    self.velocities.append(velocity)

                    self.pbest_positions.append(position.copy())
                    self.pbest_values.append(float('inf'))
                    break
            else:
                self.logger.info(
                    f"Cannot initial particle {i + 1} satisfy max_prefill_batch_size <= maxbatchsize condition, "
                    f"please ensure that the search range for max_batch_size overlaps with "
                    f"or is greater than the search range for max_prefill_batch_size.")
                return

    def set_parameter_ranges(self, ranges: dict) -> None:
        for _, (low, high, _) in ranges.items():
            self.params_bounds.append([low, high])

    def set_constraints(self, constraints: dict) -> None:
        self.constraints = constraints


    def has_next(self):
        if self.current_particle == len(self.particles) and self.current_iteration == self.n_iterations - 1:
            return False
        if self.current_particle == len(self.particles):
            self.current_particle = 0
            self.current_iteration += 1
        return True

    def get_next_params(self):
        if self.current_particle == self.current_iteration == 0:
            self.init_particles()
        else:
            w = 0.7
            c1 = 1.5
            c2 = 1.5
            for j in range(len(self.particles[self.current_particle])):
                r1 = random.random()
                r2 = random.random()
                cognitive_component = c1 * r1 * (
                        self.pbest_positions[self.current_particle][j] - self.particles[self.current_particle][j]
                )
                social_component = c2 * r2 * (
                        self.gbest_position[j] - self.particles[self.current_particle][j]
                ) if self.gbest_position is not None else 0
                self.velocities[self.current_particle][j] = (
                        w * self.velocities[self.current_particle][j]
                        + cognitive_component
                        + social_component
                )
                self.particles[self.current_particle][j] = (
                        self.particles[self.current_particle][j] + self.velocities[self.current_particle][j]
                )

                self.particles[self.current_particle][j] = (
                    np.clip(
                        self.particles[self.current_particle][j], self.params_bounds[j][0], self.params_bounds[j][1]
                    )
                )

        concurrency = self.particles[self.current_particle][0]
        req_rate = self.particles[self.current_particle][1]
        max_prefill_batch_size = self.particles[self.current_particle][2]
        prefill_time_ms_per_req = self.particles[self.current_particle][3]
        maxbatchsize = self.particles[self.current_particle][4]

        self.tune_params = {
                'concurrency': int(round(concurrency)),
                'request_rate': round(req_rate, 2),
                'max_prefill_batch_size': int(round(max_prefill_batch_size)),
                'prefill_time_ms_per_request': int(round(prefill_time_ms_per_req)),
                'max_batch_size': int(round(maxbatchsize))
            }

        return self.tune_params

    def update(self, params: dict, result: dict):
        meets_thresholds = True
        constraint_violation = 0
        violations = {}
        avg_prefill_delay_threshold = self.constraints['avg_prefill_time_thld']
        avg_decode_delay_threshold = self.constraints['avg_decode_time_thld']
        slo_p90_prefill_delay_threshold = self.constraints['SLOP90_prefill_time_thld']
        slo_p90_decode_delay_threshold = self.constraints['SLOP90_decode_time_thld']

        thp = result['performance']
        avg_prefill_delay = result['avg_prefill_time']
        avg_decode_delay = result['avg_decode_time']
        p90_prefill_delay = result['p90_prefill_time']
        p90_decode_delay = result['p90_decode_time']

        if avg_prefill_delay_threshold > 0 and avg_prefill_delay > avg_prefill_delay_threshold:
            meets_thresholds = False
            violation = avg_prefill_delay - avg_prefill_delay_threshold
            violations['avg_prefill_delay'] = violation
            constraint_violation += self.penalty_coefficients['avg_prefill_delay'] * violation
        if slo_p90_prefill_delay_threshold > 0 and p90_prefill_delay > slo_p90_prefill_delay_threshold:
            meets_thresholds = False
            violation = p90_prefill_delay - slo_p90_prefill_delay_threshold
            violations['p90_prefill_delay'] = violation
            constraint_violation += self.penalty_coefficients['p90_prefill_delay'] * violation
        if avg_decode_delay_threshold > 0 and avg_decode_delay > avg_decode_delay_threshold:
            meets_thresholds = False
            violation = avg_decode_delay - avg_decode_delay_threshold
            violations['avg_decode_delay'] = violation
            constraint_violation += self.penalty_coefficients['avg_decode_delay'] * violation
        if slo_p90_decode_delay_threshold > 0 and p90_decode_delay > slo_p90_decode_delay_threshold:
            meets_thresholds = False
            violation = p90_decode_delay - slo_p90_decode_delay_threshold
            violations['p90_decode_delay'] = violation
            constraint_violation += self.penalty_coefficients['p90_decode_delay'] * violation

        if meets_thresholds:
            fitness = -thp
        else:
            fitness = -thp + constraint_violation

        if fitness < self.pbest_values[self.current_particle]:
            self.pbest_positions[self.current_particle] = self.particles[self.current_particle].copy()
            self.pbest_values[self.current_particle] = fitness

        if meets_thresholds and fitness < self.gbest_value:
            self.gbest_position = self.particles[self.current_particle].copy()
            self.gbest_params = self.tune_params.copy()
            self.gbest_value = fitness
            self.gbest_thp = thp

            self.gbest_avg_prefill_delay = avg_prefill_delay
            self.gbest_p90_prefill_delay = p90_prefill_delay
            self.gbest_avg_decode_delay = avg_decode_delay
            self.gbest_p90_decode_delay = p90_decode_delay

            self.logger.info(
                f"Update global best results! The new best parameters: {self.tune_params}, best TPS: {self.gbest_thp}"
            )

        self.logger.info(
            f"Particle {self.current_particle + 1}/{len(self.particles)} "
            f"in Iter {self.current_iteration + 1}/{self.n_iterations} result:\n"
            f"Current parameters: {self.tune_params}\n"
            f"Current latencies:\n"
            f"    avg_prefill_delay={avg_prefill_delay}, p90_prefill_delay={p90_prefill_delay}, "
            f"avg_decode_delay={avg_decode_delay}, p90_decode_delay={p90_decode_delay}, thp={thp}\n"
            f"Fitness: {fitness}, satisfy condition: {meets_thresholds}\n"
            f"Current best parameters:\n"
            f"    {self.gbest_params}\n"
            f"Current best TPS: {self.gbest_thp}\n"
            f"Current best parameters\' latencies:\n"
            f"    avg_prefill_delay={self.gbest_avg_prefill_delay},"
            f" p90_prefill_delay={self.gbest_p90_prefill_delay},"
            f" avg_decode_delay={self.gbest_avg_decode_delay},"
            f" p90_decode_delay={self.gbest_p90_decode_delay}"
        )

        self.current_particle += 1

    def get_best_params(self):
        if self.gbest_params is None:
            self.logger.info("Cannot get one result satisfying the conditions in the iterations。")
            self.logger.info(f"=== PSO Search finished ===")
            return self.gbest_params, self.gbest_thp

        self.logger.info(f"=== PSO Search finished ===")
        self.logger.info(f"The best parameters are: {self.gbest_params}, the best TPS is: {self.gbest_thp}")
        self.logger.info(
            f"Best parameters\' latencies:\n"
            f" avg_prefill_delay={self.gbest_avg_prefill_delay},"
            f" p90_prefill_delay={self.gbest_p90_prefill_delay},"
            f" avg_decode_delay={self.gbest_avg_decode_delay},"
            f" p90_decode_delay={self.gbest_p90_decode_delay}"
        )
        return self.gbest_params, self.gbest_thp


class ParameterSolver:
    def __init__(self, config, strategy: OptimizationStrategy):
        self.config = config
        self.strategy = strategy
        self.logger = Logger.get_logger()

    def create_sim(self, **kwargs):
        config = copy.deepcopy(self.config)
        config.update(kwargs)
        sim = ServiceSimulator(config)
        return sim

    def set_ranges(self, ranges: dict):
        self.strategy.set_parameter_ranges(ranges)

    def set_constraints(self, constraints: dict):
        self.strategy.set_constraints(constraints)

    def run_solver(self):
        while self.strategy.has_next():
            params = self.strategy.get_next_params()
            if params['max_prefill_batch_size'] > params['max_batch_size']:
                self.logger.info('Cause max_prefill_batch_size > max_batch_size, skip the particle')
                continue
            self.logger.info(f'Parameter combination for next step is: {params}')
            sim = self.create_sim(**params)
            sim.run_schedule()
            result = sim.get_sim_results()
            self.logger.info(f'Current results: {result}')
            self.strategy.update(params, result)
        return self.strategy.get_best_params()