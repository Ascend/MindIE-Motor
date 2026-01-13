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
from abc import ABC, abstractmethod
from typing import List
from collections import defaultdict

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import leastsq

from mindiesimulator.common.utils import Logger
from mindiesimulator.common.path_check import PathCheck

PREFILL = 'Prefill'
DECODE = 'Decode'
ACTION_TYPE_NONE = "action_type_none"
EXEC_TYPE = 'exec_type'
BATCH_ENTRIES = 'batch_entries'


class LatencyModelStrategy(ABC):
    @abstractmethod
    def prefill_model_time_fit(self, x: List, y: np.ndarray) -> None:
        pass

    @abstractmethod
    def prefill_frame_time_fit(self, x: np.ndarray, y: np.ndarray) -> None:
        pass

    @abstractmethod
    def decode_model_time_fit(self, x: List, y: np.ndarray) -> None:
        pass

    @abstractmethod
    def decode_frame_time_fit(self, x: np.ndarray, y: np.ndarray) -> None:
        pass

    @abstractmethod
    def prefill_model_time_predict(self, x: np.ndarray) -> float:
        pass

    @abstractmethod
    def prefill_frame_time_predict(self, x: np.ndarray) -> float:
        pass

    @abstractmethod
    def decode_model_time_predict(self, x: np.ndarray) -> float:
        pass

    @abstractmethod
    def decode_frame_time_predict(self, x: np.ndarray) -> float:
        pass

    @abstractmethod
    def save_model(self):
        pass

    @abstractmethod
    def load_model(self):
        pass


class PolyFitter(LatencyModelStrategy):
    def __init__(self, save_path, dp=1):
        self.prefill_model_coefficients = None
        self.decode_model_coefficients = None
        self.prefill_frame_coefficients = None
        self.decode_frame_coefficients = None
        self.fitting_para_file = None
        self.plot_fit = False
        self.poly_coefficients_path = os.path.join(save_path, 'poly_coefficients.json')
        self.logger = Logger.get_logger()

        try:
            self.dp = max(int(dp), 1)
        except ValueError as e:
            raise ValueError("dp must be an integer or convertible to integer") from e

    def prefill_model_time_fit(self, x: List, y: np.ndarray) -> None:
        def fun(p, all_x):
            a, b, c, d = p
            result = np.zeros(len(all_x))
            epsilon = 1e-9

            safe_dp = self.dp

            try:
                for i, batch_input_lens in enumerate(all_x):
                    x_np = np.array(batch_input_lens)

                    if x_np.size == 0:
                        result[i] = a
                        continue

                    total_token = np.sum(x_np)

                    term1_denominators = 1 + d / (x_np**2 + epsilon)
                    term1_numerators = c * (x_np**2)
                    term1 = np.sum(term1_numerators / term1_denominators) * np.ceil(len(x_np) / safe_dp) / len(x_np)

                    term2_denominator = 1 + d / (total_token + epsilon)
                    term2_numerator = b * total_token
                    term2 = term2_numerator / term2_denominator

                    result[i] = a + term1 + term2

            except FloatingPointError:
                self.logger.error(
                     f"Floating point error during prefill fit calculation for batch {i}."
                     f"Inputs: x={batch_input_lens}, params={p}, dp={self.dp}"
                )
                result[i] = np.nan
            except Exception as e:
                self.logger.error(
                    f"Unexpected error during prefill fit calculation for batch {i}: {e}."
                    f"Inputs: x={batch_input_lens}, params={p}, dp={self.dp}"
                )
                result[i] = np.nan

            return result

        def error(p, x, y):
            return fun(p, x) - y

        p0 = np.array([1, 1, 1, 1])
        self.prefill_model_coefficients = leastsq(error, p0, args=(x, y))[0]
        if self.plot_fit:
            y_fitted = fun(self.prefill_model_coefficients.tolist(), x)
            plt.figure()
            plt.plot(np.linspace(1, len(x), len(x)), y, 'r', label='Prefill stage')
            plt.plot(np.linspace(1, len(x), len(x)), y_fitted, '-b', label='Fitted curve')
            plt.xlabel('Step')
            plt.ylabel('Prefill model time (ms)')
            plt.legend()
            plt.savefig('prefill_model_fit.png')
            plt.close()

    def decode_model_time_fit(self, x: List, y: np.ndarray) -> None:
        def fun(p, all_x):
            a, b, c, d = p
            result = np.zeros(len(all_x))
            try:
                for i, x in enumerate(all_x):
                    x = np.array(x)
                    bsz = len(x)
                    result[i] = a + b * bsz + c * np.sum(x / (1 + d / x))
            except ZeroDivisionError as e:
                raise ValueError('The divisor cannot be zero in decode_model_time_fit') from e
            return result

        def error(p, x, y):
            return fun(p, x) - y

        p0 = np.array([1, 1, 1, 1])
        self.decode_model_coefficients = leastsq(error, p0, args=(x, y))[0]

        if self.plot_fit:
            y_fitted = fun(self.decode_model_coefficients.tolist(), x)
            plt.figure()
            plt.plot(np.linspace(1, len(x), len(x)), y, 'r', label='Prefill stage')
            plt.plot(np.linspace(1, len(x), len(x)), y_fitted, '-b', label='Fitted curve')
            plt.xlabel('Step')
            plt.ylabel('Decode model time (ms)')
            plt.legend()
            plt.savefig('decode_model_fit.png')
            plt.close()

    def prefill_frame_time_fit(self, x: np.ndarray, y: np.ndarray) -> None:
        def fun(p, x):
            a, b, c = p
            result = a + b * x[:, 0] + c * x[:, 1]
            return result

        def error(p, x, y):
            return fun(p, x) - y

        p0 = np.array([1, 1, 1])
        self.prefill_frame_coefficients = leastsq(error, p0, args=(x, y))[0]
        if self.plot_fit:
            y_fitted = fun(self.prefill_frame_coefficients.tolist(), x)
            plt.figure()
            plt.plot(np.linspace(1, len(x), len(x)), y, 'r', label='Prefill stage')
            plt.plot(np.linspace(1, len(x), len(x)), y_fitted, '-b', label='Fitted curve')
            plt.xlabel('Step')
            plt.ylabel('Prefill frame time (ms)')
            plt.legend()
            plt.savefig('prefill_frame_fit.png')
            plt.close()

    def decode_frame_time_fit(self, x: np.ndarray, y: np.ndarray) -> None:
        def fun(p, x):
            a, b, c = p
            result = a + b * x[:, 0] + c * x[:, 1]
            return result

        def error(p, x, y):
            return fun(p, x) - y

        p0 = np.array([1, 1, 1])
        self.decode_frame_coefficients = leastsq(error, p0, args=(x, y))[0]
        if self.plot_fit:
            y_fitted = fun(self.decode_frame_coefficients.tolist(), x)
            plt.figure()
            plt.plot(np.linspace(1, len(x), len(x)), y, 'r', label='Prefill stage')
            plt.plot(np.linspace(1, len(x), len(x)), y_fitted, '-b', label='Fitted curve')
            plt.xlabel('Step')
            plt.ylabel('Decode frame time (ms)')
            plt.legend()
            plt.savefig('decode_frame_fit.png')
            plt.close()

    def save_model(self):
        # Check if coefficients are None (indicating fitting failed) before saving
        coefficient_present = {
            self.prefill_model_coefficients is not None or \
            self.prefill_frame_coefficients is not None or \
            self.decode_model_coefficients is not None or \
            self.decode_frame_coefficients is not None
        }
        if not coefficient_present:
            self.logger.error("Cannot save model coefficients, fitting was unsuccessful or coefficients are missing.")
            raise RuntimeError("Attempted to save model with missing coefficients.")

        coefficients = {
            'prefill_model_coefficients': self.prefill_model_coefficients.tolist(),
            'prefill_frame_coefficients': self.prefill_frame_coefficients.tolist(),
            'decode_model_coefficients': self.decode_model_coefficients.tolist(),
            'decode_frame_coefficients': self.decode_frame_coefficients.tolist(),
            'dp_used_for_fitting': self.dp
        }
        try:
            with PathCheck.safe_open(self.poly_coefficients_path, 'w') as file:
                json.dump(coefficients, file, indent=4)
            self.logger.info(f"Polynomial coefficients saved to {self.poly_coefficients_path}")
        except Exception as e:
            self.logger.error(f"Failed to save polynomial coefficients: {e}")
            raise IOError(f"Failed to save coefficients to {self.poly_coefficients_path}") from e

    def load_model(self):
        try:
            with PathCheck.safe_open(self.poly_coefficients_path, 'r') as file:
                json_data = json.load(file)
            self.prefill_model_coefficients = np.array(json_data['prefill_model_coefficients'])
            self.prefill_frame_coefficients = np.array(json_data['prefill_frame_coefficients'])
            self.decode_model_coefficients = np.array(json_data['decode_model_coefficients'])
            self.decode_frame_coefficients = np.array(json_data['decode_frame_coefficients'])

        except FileNotFoundError:
            self.logger.error(f"Coefficient file not found: {self.poly_coefficients_path}")
            raise
        except KeyError as e:
            self.logger.error(f"Missing key {e} in coefficient file: {self.poly_coefficients_path}")
            raise ValueError(f"Invalid coefficient file format: missing key {e}") from e
        except Exception as e:
            self.logger.error(f"Failed to load polynomial coefficients: {e}")
            raise IOError(f"Failed to load coefficients from {self.poly_coefficients_path}") from e

    def prefill_model_time_predict(self, x) -> float:
        try:
            x_np = np.array(x)
            input_lengths = x_np[:, 1].astype(float)
        except (IndexError, ValueError) as e:
            try:
                self.logger.error(f"Error extracting input lengths from input x: {e}")
            except AttributeError as e_attr:
                raise AttributeError(f"ERROR: Error extracting input lengths from input x: {e}") from e_attr
            return np.nan

        try:
            a, b, c, d = self.prefill_model_coefficients
        except (AttributeError, IndexError, TypeError):
            try:
                self.logger.error("Prefill model coefficients not found or invalid.")
            except AttributeError as e_attr:
                raise AttributeError("ERROR: Prefill model coefficients not found or invalid.") from e_attr
            return np.nan

        epsilon = 1e-9
        safe_dp = self.dp
        num_sequences = len(input_lengths)

        if num_sequences == 0:
            return max(0.0, a / 1000.0)

        try:
            total_token = np.sum(input_lengths)
            input_lengths_sq = input_lengths**2
            term1_denominators_sum = 1 + d / (input_lengths_sq + epsilon)
            term1_numerators_sum = c * input_lengths_sq
            scaling_factor = np.ceil(num_sequences / safe_dp) / num_sequences
            term1 = np.sum(term1_numerators_sum / term1_denominators_sum) * scaling_factor

            term2_denominator = 1 + d / (total_token + epsilon)
            term2_numerator = b * total_token
            term2 = term2_numerator / term2_denominator

            predicted_time_ms = a + term1 + term2

        except FloatingPointError as e:
            try:
                self.logger.error(
                    f"Floating point error during prefill prediction: {e}."
                    f"Inputs lengths: {input_lengths}, params=[a={a},b={b},"
                    f"c={c},d={d}], dp={safe_dp}"
                )
            except AttributeError as e_attr:
                raise AttributeError(
                    f"ERROR: Floating point error during prefill prediction: {e}."
                    f"Inputs lengths: {input_lengths}, params=[a={a},b={b},c={c},"
                    f"d={d}], dp={safe_dp}"
                ) from e_attr
            return np.nan # Return NaN on numerical errors
            
        except Exception as e: # Catch other potential errors during calculation
            try:
                self.logger.error(
                    f"Unexpected error during prefill prediction calculation: {e}." 
                    f"Inputs lengths: {input_lengths}, params=[a={a},b={b},c={c},"
                    f"d={d}], dp={safe_dp}"
                    )
            except AttributeError as e_attr:
                raise AttributeError(
                    f"ERROR: Unexpected error during prefill prediction calculation: {e}." 
                    f"Inputs lengths: {input_lengths}, params=[a={a},b={b},c={c},d={d}],"
                    f" dp={safe_dp}"
                    ) from e_attr
            return np.nan

        predicted_time_s = max(0.0, predicted_time_ms / 1000.0)

        return predicted_time_s

    def decode_model_time_predict(self, x: np.ndarray) -> float:
        decode_model_time = self.decode_model_coefficients[0]
        bsz = len(x)
        total_token = 0
        try:
            for i in x:
                # We suppose the KV Cache for finished requests will be removed immediately
                if i[2] < i[3]:
                    token = i[1] + i[2]
                    total_token += token
                    decode_model_time += self.decode_model_coefficients[2] * token / \
                                         (1 + self.decode_model_coefficients[3] / token)

            decode_model_time += self.decode_model_coefficients[1] * bsz
        except ZeroDivisionError as e:
            raise ValueError('The divisor cannot be zero in decode_model_time_predict.') from e
        if decode_model_time <= 0:
            decode_model_time = 0
            self.logger.debug('Negative decode model time occurs.')
        return decode_model_time / 1000

    def prefill_frame_time_predict(self, x: np.ndarray) -> float:
        prefill_frame_time = self.prefill_frame_coefficients[0]
        total_token = 0
        for i in x:
            if i[2] <= 0:
                raise ValueError('Decode length must larger than 0!')
            prefill_frame_time += self.prefill_frame_coefficients[1]
            total_token += i[1]
        prefill_frame_time += self.prefill_frame_coefficients[2] * total_token
        if prefill_frame_time <= 0:
            prefill_frame_time = 0
            self.logger.debug('Negative prefill frame time occurs.')
        return prefill_frame_time / 1000

    def decode_frame_time_predict(self, x: np.ndarray) -> float:
        decode_frame_time = self.decode_frame_coefficients[0]
        total_token = 0
        for i in x:
            # Suppose the KV Cache for finished requests will be removed immediately
            if i[2] < i[3]:
                decode_frame_time += self.decode_frame_coefficients[1]
        decode_frame_time += self.decode_frame_coefficients[2] * total_token
        if decode_frame_time <= 0:
            decode_frame_time = 0
            self.logger.debug('Negative decode frame time occurs.')
        return decode_frame_time / 1000


class LatencyModeling:
    def __init__(self, data_path, fit_strategy: LatencyModelStrategy):
        self.req_to_data_map = None
        self.batch_info = None
        self.results_per_request = None
        self.latency_data = None
        self.log_data_path = data_path
        self.input_len = []
        self.output_len = []
        self.all_pd_info = []
        self.full_data = []
        self.frame_time_type = 'median'
        self.plot = 'All'
        self.strategy = fit_strategy
        self.logger = Logger.get_logger()

    def get_latency_data(self):
        with PathCheck.safe_open(os.path.join(self.log_data_path, 'latency_data.json'), 'r', encoding='utf-8') as f:
            self.latency_data = json.load(f)

    def set_strategy(self, fit_strategy: LatencyModelStrategy):
        self.strategy = fit_strategy

    def extract_input_output_len(self):
        results_per_request = self.latency_data['results_per_requst']
        for req_id in range(len(results_per_request)):
            self.input_len.append(results_per_request[str(req_id)]['input_len'])
            self.output_len.append(results_per_request[str(req_id)]['output_len'])

    def save_input_output_len(self):
        input_output_len_dic = {
            "input_lens": self.input_len,
            "output_lens": self.output_len
        }
        with PathCheck.safe_open(
                os.path.join(
                    self.log_data_path, 'input_output_len.json'
                ), 'w', encoding='utf-8'
        ) as file:
            json.dump(input_output_len_dic, file, indent=4)

    def parse_data(self):
        results_per_request = self.latency_data['results_per_requst']
        req_data_map = self.latency_data['req_data_map']
        batch_info = self.latency_data['batch_info']

        batches = defaultdict(lambda: {EXEC_TYPE: ACTION_TYPE_NONE, BATCH_ENTRIES: []})

        for record in batch_info:

            if 'warm_up' in record['request_id']:
                continue

            batch_id = record['batch_id']
            if batches[batch_id][EXEC_TYPE] == ACTION_TYPE_NONE:
                batches[batch_id][EXEC_TYPE] = PREFILL if record['is_prefill'] is True else DECODE

            batches[batch_id][BATCH_ENTRIES].append(record)

        for _, info in batches.items():
            entries = info[BATCH_ENTRIES]
            exec_type = info[EXEC_TYPE]
            bsz = len(entries)
            model_spend_time = entries[0]['generate_token']

            req_info = []
            for e in entries:
                request_id = e['request_id']
                token_idx = e['token_idx']
                decode_step = token_idx - 1
                req_data = results_per_request.get(req_data_map[request_id], {})
                input_len = req_data.get('input_len', 1)
                req_latency_list = req_data.get('latency', [])
                req_queue_latancy_list = req_data.get('queue_latency', [])
                latency_ms = req_latency_list[decode_step]
                queue_latency_ms = req_queue_latancy_list[decode_step] / 1000.0

                req_info.append([
                    input_len + decode_step,
                    latency_ms,
                    queue_latency_ms
                ])

            self.all_pd_info.append([
                exec_type,
                bsz,
                model_spend_time,
                req_info
            ])

    def get_frametime(self):
        after_prefill = True
        for index, info in enumerate(self.all_pd_info):
            table = np.array(info[-1])
            latency = table[:, 1] - table[:, 2]
            if self.frame_time_type == 'mean':
                latency = np.mean(latency)
            elif self.frame_time_type == 'max':
                latency = np.max(latency)
            elif self.frame_time_type == 'median':
                latency.sort()
                latency = latency[len(latency) // 2]
            else:
                raise RuntimeError('Unknown Frame Time Type!')
            frame_time = latency - info[2]

            if info[0] == PREFILL:
                frame_time_litied = 1000
            else:
                frame_time_litied = 100

            if frame_time <= 0 or frame_time > frame_time_litied:
                self.logger.debug(
                    f"Batch {index} encountered negative {info[0]} frame time - {frame_time}"
                    )
            else:
                # time_data_tmp [stage, bsz, model time, total time, frame time, sequence length list]
                time_data_tmp = [info[0],
                        info[1],
                        info[2],
                        frame_time,
                        list(table[:, 0])
                        ]
                self.full_data.append(time_data_tmp)

        if self.plot is not None:
            if self.plot in ['Prefill', 'All']:
                y = []
                #  [stage, bsz, model time, frame time, decode table]
                for _, info in enumerate(self.all_pd_info):
                    if info[0] == 'Prefill':
                        y.append(info[1])
                x = np.arange(len(y))
                plt.xlabel('Step')
                plt.ylabel('Prefill Batch Size')
                plt.scatter(x[:], y[:], s=2)
                plt.show()

            if self.plot in ['Decode', 'All']:
                y = []
                for _, info in enumerate(self.all_pd_info):
                    if info[0] == 'Decode':
                        y.append(info[1])
                x = np.arange(len(y))
                plt.xlabel('Step')
                plt.ylabel('Decode Batch size')
                plt.scatter(x[:], y[:], s=2)
                plt.show()

    def prefill_model_time_fit(self) -> None:
        new_info = self.full_data
        x = []
        y = []
        for _, info in enumerate(new_info):
            if info[0] == PREFILL:
                x.append(info[-1])
                y.append(info[2])
        y = np.array(y)
        self.strategy.prefill_model_time_fit(x, y)

    def prefill_frame_time_fit(self) -> None:
        new_info = self.full_data
        frame_times = []
        tokens = []
        for _, info in enumerate(new_info):
            if info[0] == PREFILL:
                frame_times.append(info[3])
                tokens.append([info[1], np.sum(info[-1])])
        y = np.array(frame_times)
        x = np.array(tokens)
        self.strategy.prefill_frame_time_fit(x, y)

    def decode_model_time_fit(self) -> None:
        new_info = self.full_data
        x = []
        y = []
        for _, info in enumerate(new_info):
            if info[0] == DECODE:
                x.append(info[-1])
                y.append(info[2])
        y = np.array(y)
        self.strategy.decode_model_time_fit(x, y)

    def decode_frame_time_fit(self) -> None:
        new_info = self.full_data
        frame_times = []
        tokens = []
        for _, info in enumerate(new_info):
            if info[0] == DECODE:
                frame_times.append(info[3])
                tokens.append([info[1], sum(info[-1])])
        y = np.array(frame_times)
        x = np.array(tokens)
        self.strategy.decode_frame_time_fit(x, y)

    def prefill_model_time_predict(self, x):
        return self.strategy.prefill_model_time_predict(x)

    def prefill_frame_time_predict(self, x):
        return self.strategy.prefill_frame_time_predict(x)

    def decode_model_time_predict(self, x):
        return self.strategy.decode_model_time_predict(x)

    def decode_frame_time_predict(self, x):
        return self.strategy.decode_frame_time_predict(x)

    def save_model(self) -> None:
        return self.strategy.save_model()

    def load_model(self) -> None:
        return self.strategy.load_model()