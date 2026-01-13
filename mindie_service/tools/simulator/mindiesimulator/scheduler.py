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
import time

import numpy as np

from mindiesimulator.inference_engine import InferenceSim
from mindiesimulator.request_sender import RequestStrategy

from mindiesimulator.common.utils import Logger

DURATION_TIME = "duration_time"


class ScheduleStrategy(ABC):
    @abstractmethod
    def prefill(self):
        pass

    @abstractmethod
    def decode(self):
        pass

    @abstractmethod
    def run(self):
        pass

OUTPUT_LEN = 'output_len'
LATENCY = 'latency'


class PDConflictScheduler(ScheduleStrategy):
    def __init__(self, config, inference_engine, sim_situation='Frequency'):
        self.sim_situation = sim_situation
        self.flag_print = False
        self.first_schedule = None
        self.min_prefill_batch = 0
        self.max_prefill_batch_size = config.get('max_prefill_batch_size')
        self.model_name = config.get('model_name')
        self.max_batch_size = config.get('max_batch_size')
        self.concurrency = config.get('concurrency')
        self.max_iterations = 10000
        self.max_prefill_tokens = config.get('max_prefill_tokens')
        self.max_sequence_length = config.get('max_seq_len')
        self.max_input_length = config.get('max_input_len')
        self.prefill_time_ms_per_request = config.get('prefill_time_ms_per_request')
        self.decode_time_ms_per_request = config.get('decode_time_ms_per_request')
        self.select_batch = config.get('support_select_batch')
        self.inference_engine = inference_engine
        self.first_schedule = 'Decode' if self.select_batch is True else 'Prefill'
        self.req_idx = 0
        self.flag_recomp = config.get('recompute_ratio') > 0
        self.used_kv_block = 0
        self.curr_time = 0
        self.min_prefill_batch = 0
        # request_in_dataset -> waiting_pool -> running_pool -> decode_table -> finished_request
        self.waiting_pool = []  # [[prompt length, target decode length],...]
        self.running_pool = []  # [[id, prompt length, target decode length, 0],...]
        self.decode_table = []  # [[id, prompt length, current decode length, target decode length, decode time],...]
        self.finished_request = []  # [id, ...]
        self.request_in_dataset = []  # [[prompt length, target decode length],...]
        self.num_request = 0
        self.swapped_list = []  # [[id, prompt length, current decode length, target decode length, decode time],...]

        self.prefill_time_bsz = []
        self.decode_time_bsz = []
        self.cost_d = 1e20
        self.result_caching = []

        self.logger = Logger.get_logger()

    def add_request2result_caching(self, iid, input_len, added_time):
        self.result_caching.append(
            {
                'id': iid,
                'input_len': input_len,
                'output_len': None,
                'latency': [],
                'queue_wait_time': [],
                'batch_size': [],
                'req_latency': None,
                'start_time': added_time,
                'end_time': None,
                'duration_time': added_time
            }
        )

    def run(self):
        # only one step, either prefill or decode
        if self.flag_print:
            self.logger.info('current time: %s', self.curr_time)

        # Prefill
        if len(self.running_pool) > self.min_prefill_batch and len(self.decode_table) < self.concurrency:
            if self.prefill():
                return False

        # Decode
        if len(self.decode_table) > 0:
            if self.decode():
                return True

        return False

    def prefill(self):
        max_batch_prefill = min(len(self.running_pool), self.max_prefill_batch_size)
        for batch_prefill in range(max_batch_prefill, self.min_prefill_batch, -1):
            all_prefill_token = 0
            for task in self.running_pool[:batch_prefill]:
                all_prefill_token += task[1]
            if all_prefill_token > self.max_prefill_tokens:
                continue
            if self.assert_hbm_mem(self.running_pool[:batch_prefill]):
                continue
            cost_p = self.prefill_time_ms_per_request * len(
                self.decode_table) if self.first_schedule == 'Decode' else -1
            if self.cost_d >= cost_p:
                prefill_time = self.inference_engine.prefill_sim(self.running_pool[:batch_prefill])
                self.prefill_time_bsz.append([self.curr_time, batch_prefill, prefill_time])

                for task in self.running_pool[:batch_prefill]:
                    self.result_caching[task[0]]['latency'].append(
                        self.curr_time - self.result_caching[task[0]][DURATION_TIME] + prefill_time)
                    self.result_caching[task[0]]['queue_wait_time'].append(
                        self.curr_time - self.result_caching[task[0]][DURATION_TIME])
                    self.result_caching[task[0]][DURATION_TIME] = self.curr_time + prefill_time
                    self.result_caching[task[0]]['batch_size'].append(batch_prefill)

                    self.decode_table.append([task[0], task[1], 1, task[2]])

                self.curr_time += prefill_time
                self.running_pool = self.running_pool[batch_prefill:]

                self.assert_decode_finish()

                if self.flag_print:
                    self.logger.info("Prefill BS: %s", batch_prefill)
                self.cost_d = 0
                return True
        return False

    def assert_hbm_mem(self, prefill_table=None):
        prefill_table = prefill_table or []
        return self.inference_engine.judge_kv_block(prefill_table, decode_used_block=self.used_kv_block)[0]

    def assert_decode_finish(self):
        new_decode_table = []
        finished_num = 0
        for task in self.decode_table:
            if task[2] < task[3] and task[1] + task[2] <= self.max_sequence_length and task[2] <= self.max_iterations:
                new_decode_table.append(task)
            else:
                self.finished_request.append(task[0])
                self.result_caching[task[0]]['output_len'] = (
                    task[2] + task[1] - self.result_caching[task[0]]['input_len']
                )
                if self.result_caching[task[0]]['output_len'] < 0:
                    raise RuntimeError('Abnormal output length!')
                self.result_caching[task[0]]['end_time'] = self.curr_time
                self.result_caching[task[0]]['req_latency'] = (
                    self.curr_time - self.result_caching[task[0]]['start_time']
                )
                finished_num += 1

        if finished_num and self.flag_print:
            self.logger.info('some request are finished: %s', finished_num)
        self.decode_table = new_decode_table
        _, self.used_kv_block = self.inference_engine.judge_kv_block(decode_table=self.decode_table)

        if self.sim_situation == 'Concurrency':
            num_free_pool = self.concurrency - len(self.decode_table) - len(self.running_pool)
            self.add_request(num_free_pool, self.curr_time)
        self.update_pool()

    def update_pool(self):
        while len(self.waiting_pool) > 0 and self.assert_pool():
            self.running_pool.append([self.req_idx] + self.waiting_pool.pop(0))
            self.add_request2result_caching(self.req_idx, self.running_pool[-1][1], self.curr_time)
            self.req_idx += 1

    def decode(self):
        if self.judge_trigger_swap_recomp():
            decode_table = self.inference_engine.construct_decode_batch(self.max_batch_size, self.decode_table)
            if len(decode_table) < 1:
                return True
        else:
            decode_table = self.decode_table[:self.max_batch_size]

        inference_list = [i[0] for i in decode_table]
        if self.first_schedule == 'Decode':
            self.cost_d += self.decode_time_ms_per_request * max(self.max_batch_size - len(decode_table), 0)
        if len(self.running_pool) == 0:
            self.cost_d = 0
        decode_time = self.inference_engine.decode_sim(decode_table)

        self.decode_time_bsz.append([self.curr_time, len(decode_table), decode_time])
        for j, i in enumerate(self.decode_table):
            if i[0] in inference_list:
                self.result_caching[i[0]]['latency'].append(
                    self.curr_time - self.result_caching[i[0]][DURATION_TIME] + decode_time)
                self.result_caching[i[0]]['queue_wait_time'].append(
                    self.curr_time - self.result_caching[i[0]][DURATION_TIME]
                )
                self.result_caching[i[0]][DURATION_TIME] = self.curr_time + decode_time
                self.result_caching[i[0]]['batch_size'].append(len(decode_table))
                self.decode_table[j][2] += 1

        if self.flag_print:
            self.logger.info("Decode BS:", len(decode_table))

        self.curr_time += decode_time
        self.assert_decode_finish()
        return False

    def judge_trigger_swap_recomp(self):

        hbm_flag, self.used_kv_block = self.inference_engine.judge_kv_block([], self.decode_table, None, True)
        if self.flag_recomp and hbm_flag:
            self.decode_table, recompute_pool, recompute_time = self.inference_engine.recompute(self.decode_table)
            self.curr_time += recompute_time
            self.running_pool = recompute_pool + self.running_pool
            hbm_flag, self.used_kv_block = self.inference_engine.judge_kv_block([], self.decode_table, None, True)

        if not hbm_flag:
            return False

        return hbm_flag

    def get_avg_throughput(self):
        all_tokens = 0
        for i in self.result_caching:
            if i['output_len'] is not None:
                all_tokens += i['output_len']
        for i in self.decode_table:
            all_tokens += i[2]

        throughput = all_tokens / self.curr_time
        return throughput

    def get_avg_decode_throughput(self):
        all_tokens = 0
        for i in self.result_caching:
            if i['output_len'] is not None:
                all_tokens += i['output_len'] - 1

        for i in self.decode_table:
            all_tokens += i[2] - 1

        throughput = all_tokens / self.curr_time
        return throughput

    def get_avg_request_used_time(self):
        return self.get_num_finished_request() / self.curr_time

    def get_finished_request(self):
        return self.finished_request

    def get_num_finished_request(self):
        return len(self.finished_request)

    def assert_pool(self):
        return len(self.decode_table) + len(self.running_pool) + len(self.swapped_list) < self.concurrency

    def add_request(self, num, added_time):
        for _ in range(num):
            if len(self.request_in_dataset) > 0:
                self.add_one_request(self.request_in_dataset.pop(0), added_time)
        self.update_pool()

    def add_one_request(self, request, added_time):
        if request[0] > self.max_input_length or request[0] > self.max_sequence_length:
            return

        if len(self.waiting_pool) > 0 or not self.assert_pool():
            self.waiting_pool.append(request)
        else:
            self.running_pool.append([self.req_idx] + request)
            self.add_request2result_caching(self.req_idx, request[0], added_time)
            self.req_idx += 1

    def add_dataset_request(self, request):
        if isinstance(request[0], list):
            self.request_in_dataset += request
        else:
            self.request_in_dataset.append(request)
        self.num_request = len(self.request_in_dataset)

    def assert_load(self):
        conditions = [
            len(self.decode_table) > 0,
            len(self.waiting_pool) > 0,
            len(self.running_pool) > 0,
            len(self.swapped_list) > 0
        ]
        return any(conditions)

    def set_curr_time(self, curr_time):
        self.curr_time = curr_time

    def get_curr_time(self):
        return self.curr_time

    def get_num_non_empty_req(self):
        num_non_empty_req = 0
        for i in self.result_caching:
            if i['output_len'] is not None and i['output_len'] > 1:
                num_non_empty_req += 1
        return num_non_empty_req

    def print_detail_info(self):
        input_len = []
        output_len = []
        prefill_latency = []
        decode_latency = []
        last_decode_latency = []
        max_decode_latency = []
        prefill_bsz = []
        decode_bsz = []
        queue_wait_time = []
        generate_time = []
        generated_token_speed = []
        decode_latency_per_request = []
        decode_bsz_per_request = []
        queue_wait_time_per_request = []
        for i in self.prefill_time_bsz:
            prefill_bsz.append(i[1])
        for i in self.result_caching:
            if i[OUTPUT_LEN] is None:
                raise RuntimeError('Unfinished Request Detected! Request: ' + str(i))
            input_len.append(i['input_len'])
            output_len.append(i[OUTPUT_LEN])
            generate_time.append(i['req_latency'])
            generated_token_speed.append(i[OUTPUT_LEN] / i['req_latency'])
            prefill_latency.append(i[LATENCY][0])
            queue_wait_time.extend(i['queue_wait_time'])
            queue_wait_time_per_request.append(np.mean(i['queue_wait_time']))
            if i[OUTPUT_LEN] > 1:
                decode_latency.extend(i[LATENCY][1:])
                decode_latency_per_request.append(np.mean(i[LATENCY][1:]))
                decode_bsz.extend(i['batch_size'][1:])
                decode_bsz_per_request.append(np.mean(i['batch_size'][1:]))
                last_decode_latency.append(i[LATENCY][-1])
                max_decode_latency.append(max(i[LATENCY][1:]))
        table_border = ('+---------------------+---------------------+----------------------+----------------------+'
                        '----------------------+----------------------+----------------------+----------------------+')
        self.logger.info(table_border)
        self.logger.info(
            '|              Metric |             average |                  max |                  min |'
            '                  p75 |                  p90 |              slo_p90 |                  p99 |')
        self.logger.info(table_border)
        self.logger.info(
            '|      FirstTokenTime |%12.4f      ms | %12.4f      ms | %12.4f      ms | %12.4f      ms |'
            ' %12.4f      ms | %12.4f      ms | %12.4f      ms |' % (
                np.mean(prefill_latency) * 1000, max(prefill_latency) * 1000, min(prefill_latency) * 1000,
                np.percentile(prefill_latency, 75) * 1000, np.percentile(prefill_latency, 90) * 1000,
                np.percentile(prefill_latency, 90) * 1000, np.percentile(prefill_latency, 99) * 1000))
        self.logger.info(
            '|          DecodeTime |%12.4f      ms | %12.4f      ms | %12.4f      ms | %12.4f      ms |'
            ' %12.4f      ms | %12.4f      ms | %12.4f      ms |' % (
                np.mean(decode_latency) * 1000, max(decode_latency) * 1000, min(decode_latency) * 1000,
                np.percentile(decode_latency, 75) * 1000, np.percentile(decode_latency, 90) * 1000,
                np.percentile(decode_latency_per_request, 90) * 1000, np.percentile(decode_latency, 99) * 1000))
        self.logger.info(
            '|      LastDecodeTime |%12.4f      ms | %12.4f      ms | %12.4f      ms | %12.4f      ms |'
            ' %12.4f      ms | %12.4f      ms | %12.4f      ms |' % (
                np.mean(last_decode_latency) * 1000, max(last_decode_latency) * 1000, min(last_decode_latency) * 1000,
                np.percentile(last_decode_latency, 75) * 1000, np.percentile(last_decode_latency, 90) * 1000,
                np.percentile(last_decode_latency, 90) * 1000, np.percentile(last_decode_latency, 99) * 1000))
        self.logger.info(
            '|       MaxDecodeTime |%12.4f      ms | %12.4f      ms | %12.4f      ms | %12.4f      ms |'
            ' %12.4f      ms | %12.4f      ms | %12.4f      ms |' % (
                np.mean(max_decode_latency) * 1000, max(max_decode_latency) * 1000, min(max_decode_latency) * 1000,
                np.percentile(max_decode_latency, 75) * 1000, np.percentile(max_decode_latency, 90) * 1000,
                np.percentile(max_decode_latency, 90) * 1000, np.percentile(max_decode_latency, 99) * 1000))
        self.logger.info(
            '|        GenerateTime |%12.4f      ms | %12.4f      ms | %12.4f      ms | %12.4f      ms |'
            ' %12.4f      ms | %12.4f      ms | %12.4f      ms |' % (
                np.mean(generate_time) * 1000, max(generate_time) * 1000, min(generate_time) * 1000,
                np.percentile(generate_time, 75) * 1000, np.percentile(generate_time, 90) * 1000,
                np.percentile(generate_time, 90) * 1000, np.percentile(generate_time, 99) * 1000))
        self.logger.info(
            '|         InputTokens |%12.4f         | %12d         | %12d         | %12d         |'
            ' %12d         | %12d         | %12d         |' % (
                np.mean(input_len), max(input_len), min(input_len), np.percentile(input_len, 75),
                np.percentile(input_len, 90), np.percentile(input_len, 90), np.percentile(input_len, 99)))
        self.logger.info(
            '|     GeneratedTokens |%12.4f         | %12.4f         | %12.4f         | %12.4f         |'
            ' %12.4f         | %12.4f         | %12.4f         |' % (
                np.mean(output_len), max(output_len), min(output_len), np.percentile(output_len, 75),
                np.percentile(output_len, 90), np.percentile(output_len, 90), np.percentile(output_len, 99)))
        self.logger.info(
            '| GeneratedTokenSpeed |%12.4f token/s | %12.4f token/s | %12.4f token/s | %12.4f token/s |'
            ' %12.4f token/s | %12.4f token/s | %12.4f token/s |' % (
                np.mean(generated_token_speed), max(generated_token_speed), min(generated_token_speed),
                np.percentile(generated_token_speed, 75), np.percentile(generated_token_speed, 90),
                np.percentile(generated_token_speed, 90), np.percentile(generated_token_speed, 99)))
        self.logger.info(
            '|    PrefillBatchsize |%12.4f         | %12d         | %12d         | %12d         |'
            ' %12d         | %12d         | %12d         |' % (
                np.mean(prefill_bsz), max(prefill_bsz), min(prefill_bsz), np.percentile(prefill_bsz, 75),
                np.percentile(prefill_bsz, 90), np.percentile(prefill_bsz, 90),
                np.percentile(prefill_bsz, 99)))
        self.logger.info(
            '|     DecodeBatchsize |%12.4f         | %12d         | %12d         |'
            ' %12d         | %12d         | %12d         | %12d         |' % (
                np.mean(decode_bsz), max(decode_bsz), min(decode_bsz), np.percentile(decode_bsz, 75),
                np.percentile(decode_bsz, 90), np.percentile(decode_bsz_per_request, 90),
                np.percentile(decode_bsz, 99)))
        self.logger.info(
            '|       QueueWaitTime |%12.4f      us | %12.4f      us | %12.4f      us | '
            '%12.4f      us | %12.4f      us | %12.4f      us | %12.4f      us |' % (
                np.mean(queue_wait_time) * 1e6, max(queue_wait_time) * 1e6, min(queue_wait_time) * 1e6,
                np.percentile(queue_wait_time, 75) * 1e6, np.percentile(queue_wait_time, 90) * 1e6,
                np.percentile(queue_wait_time_per_request, 90) * 1e6, np.percentile(queue_wait_time, 99) * 1e6))
        self.logger.info(table_border)
        header_line = '+------------------------+----------------------+'
        generate_throughput = float(self.get_avg_decode_throughput())
        input_generate_speed = float(np.sum(input_len) / self.curr_time)

        self.logger.info('')
        self.logger.info(header_line)
        self.logger.info('|          Common Metric |                Value |')
        self.logger.info(header_line)
        self.logger.info(
            '|            CurrentTime |%21s |' % time.strftime(
                time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime()
                )
            )
        )
        self.logger.info('|            TimeElapsed |%19.4f s |' % self.curr_time)
        self.logger.info('|             DataSource |                    - |')
        self.logger.info('|                 Failed |%10d( %6.2f%% ) |' % 
        ((self.num_request - self.get_num_finished_request()), 
        (self.num_request - self.get_num_finished_request()) / self.num_request * 100))
        self.logger.info('|               Returned |%10d( %6.2f%% ) |' % (len(self.result_caching), 100))
        self.logger.info('|                  Total |%10d[ %5.2f%% ] |' % (
            self.get_num_finished_request(), self.get_num_finished_request() / len(self.result_caching) * 100))
        self.logger.info('|            Concurrency |%21d |' % self.concurrency)
        self.logger.info('|              ModelName |%21s |' % self.model_name)
        self.logger.info('|                lpct    |                    - |')
        self.logger.info('|             Throughput |%15.4f req/s |' % self.get_avg_request_used_time())
        self.logger.info('|    OutputGenerateSpeed |%13.4f token/s |' % generate_throughput)
        self.logger.info('|     InputGenerateSpeed |%13.4f token/s |' % input_generate_speed)
        self.logger.info('|     TotalGenerateSpeed |%13.4f token/s |' % (generate_throughput + input_generate_speed))
        self.logger.info('| GenerateSpeedPerClient |%13.4f token/s |' % (generate_throughput / self.concurrency))
        self.logger.info('|               Accuracy |                    - |')
        self.logger.info(header_line)

        results = {
            'performance': generate_throughput,
            'avg_prefill_time': np.mean(prefill_latency) * 1000,
            'avg_decode_time': np.mean(decode_latency) * 1000,
            'p90_prefill_time': np.percentile(prefill_latency, 90) * 1000,
            'p90_decode_time': np.percentile(decode_latency_per_request, 90) * 1000
        }
        return results


class Scheduler:
    def __init__(self, config, input_output_len, request_sender: RequestStrategy, inference_engine: InferenceSim):
        self.config = config
        self.input_output_len = input_output_len
        self.request_sender = request_sender
        self.inference_engine = inference_engine
        self.sim_result = None

    def run_schedule(self):
        req_rate = self.config.get("request_rate")
        max_prefill_batch_size = self.config.get("max_prefill_batch_size")
        req_rate = float(req_rate)
        input_lens = self.input_output_len.get('input_lens')
        output_lens = self.input_output_len.get('output_lens')
        request_dataset = [[input_lens[i], output_lens[i]] for i in range(len(input_lens))]

        if req_rate > 0:
            sim_situation = 'Frequency'
        elif req_rate == 0:
            sim_situation = 'Concurrency'
        else:
            raise RuntimeError('Unsupported req_rate!')

        schedule = PDConflictScheduler(self.config, self.inference_engine, sim_situation=sim_situation)
        schedule.add_dataset_request(request_dataset)

        if req_rate > 0:
            schedule.add_request(1, 0)
            time_add_request = self.request_sender.set_next_request_in_time(previous_time=0)
        else:
            schedule.add_request(max_prefill_batch_size, 0)

        while True:
            if req_rate > 0:
                while len(schedule.request_in_dataset) > 0 and schedule.curr_time >= time_add_request:
                    schedule.add_request(1, time_add_request)
                    time_add_request = self.request_sender.set_next_request_in_time(previous_time=time_add_request)
            if schedule.assert_load():
                if schedule.run():
                    break
            else:
                if len(schedule.request_in_dataset) > 0:
                    schedule.set_curr_time(time_add_request)
                else:
                    break

        self.sim_result = schedule.print_detail_info()
