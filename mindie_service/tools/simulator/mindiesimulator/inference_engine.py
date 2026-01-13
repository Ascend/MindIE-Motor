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

import math
import logging
import os
import re
import glob
import numpy as np

from mindiesimulator.latency_modeling import LatencyModelStrategy, LatencyModeling
from mindiesimulator.common.utils import Logger
from mindiesimulator.common.path_check import PathCheck

RECOMPUTE_TIME = 0
HBM_SIZE_OCCUPIED = 3


class InferenceSim:
    def __init__(self, config, latency_model: LatencyModelStrategy):
        self.latency_model = LatencyModeling(
            config.get("scenario"),
            latency_model
        )
        self.llm_size = config.get("llm_size")
        self.num_npu = config.get("world_size")
        self.num_hidden_layers = config.get("num_hidden_layers")
        self.hidden = config.get("hidden_size")
        self.attn_heads = config.get("num_attention_heads")
        self.kv_head = config.get("num_key_value_heads")
        self.block_size = config.get("block_size")
        self.recompute_ratio = config.get("recompute_ratio")
        self.max_recompute_num = 10
        self.npu_mem_size = config.get("npu_mem_size")
        self.hbm_size = config.get("hbm_size") - HBM_SIZE_OCCUPIED
        self.max_prefill_tokens = config.get("max_prefill_tokens")
        self.max_seq_len = config.get("max_seq_len")
        self.max_input_len = config.get("max_input_len")
        self.bytes_per_element = config.get("bytes_per_element")
        self.total_memory = self.hbm_size * 1024 ** 3
        self.logger = Logger.get_logger()
        self.dp = config.get("dp")

        if self.npu_mem_size == -1:
            if self.dp != 1:
                self.npu_mem_size = self.get_npu_mem_size_from_llm_log()
            else:
                self.npu_mem_size = self.cal_npu_mem(
                    self.max_prefill_tokens,
                    self.max_seq_len,
                    self.max_input_len,
                    self.total_memory,
                    memory_fraction=0.8
                )

        if self.num_hidden_layers == 0:
            raise ValueError('num_hidden_layers should not be zero!')
        if self.block_size == 0:
            raise ValueError('block_size should not be zero!')
        if self.kv_head == 0:
            raise ValueError('kv_head number should not be zero!')
        if self.attn_heads == 0:
            raise ValueError('attn_heads should not be zero!')
        if self.hidden == 0:
            raise ValueError('hidden size should not be zero!')
        if self.dp == 1:
            self.num_kv_block = int(
                self.npu_mem_size * 1024 ** 3 * self.num_npu / (
                    self.num_hidden_layers * self.hidden / self.attn_heads *
                    self.kv_head * 2 * 2 * self.block_size
                )
            )
        else:
            self.num_kv_block = int(
                self.npu_mem_size * 1024 ** 3 * self.dp / (
                self.num_hidden_layers * 576 * 2 * self.block_size
                )
            )

        if not self.num_npu * (self.hbm_size - self.npu_mem_size) > self.get_model_mem():
            raise ValueError('Insufficient memory for the model.')
    
    @staticmethod
    def recompute_action(decode_table):
        recompute_pool = []
        for req in decode_table:
            recompute_pool.append([req[0], req[1] + req[2], req[3] - req[2]])
        return recompute_pool
    
    def judge_kv_block(self, prefill_table=None, decode_table=None, decode_used_block=None, decode=False):
        if prefill_table is None:
            prefill_table = []
        if decode_table is None:
            decode_table = []
        kv_block = 0
        if self.block_size == 0:
            raise ValueError('block_size should not be zero!')
        for req in prefill_table:
            kv_block += np.ceil((req[1] + 1) / self.block_size)
        if decode_used_block is None:
            if decode:
                for req in decode_table:
                    kv_block += np.ceil((req[1] + req[2] + 1) / self.block_size)
            else:
                for req in decode_table:
                    kv_block += np.ceil((req[1] + req[2]) / self.block_size)
        else:
            kv_block += decode_used_block

        return kv_block > self.num_kv_block, kv_block
    
    def get_model_mem(self):
        return self.llm_size * 10 ** 9 * self.bytes_per_element / 1024 ** 3

    def cal_npu_mem(self, max_prefill_tokens, max_seq_len, max_input_len, total_memory, memory_fraction=0.8):
        if self.num_npu == 0:
            raise ValueError('num_npu should not be zero!')
        peak_memory = self.llm_size * 10 ** 9 * 2 / self.num_npu
        if self.attn_heads == 0:
            raise ValueError('attn_heads should not be zero!')
        if self.kv_head == 0:
            raise ValueError('kv_head should not be zero!')
        block_mem_size = (
            self.num_hidden_layers * self.hidden / (self.attn_heads / self.kv_head) *
            2 * 2 * self.block_size / self.num_npu
        )
        if block_mem_size == 0:
            raise ValueError('block_mem_size should not be zero!')
        total_blocks = int((total_memory * memory_fraction - peak_memory) / block_mem_size)
        peak_memory += max_prefill_tokens * self.hidden * 2 * 13.25
        prefill_blocks = 0
        max_input_length = min(max_seq_len, max_input_len)
        while max_prefill_tokens > 0:
            input_len = min(max_prefill_tokens, max_input_length)
            if self.block_size == 0:
                raise ValueError('block_size should not be zero!')
            num_required_block = math.ceil(input_len / self.block_size)
            if prefill_blocks + num_required_block > total_blocks:
                self.logger.info("The max_prefill_tokens exceeds what the NPU mem can hold.")
                break
            max_prefill_tokens -= input_len
            prefill_blocks += num_required_block
        peak_memory += prefill_blocks * block_mem_size
        if block_mem_size == 0:
            raise ValueError('block_mem_size should not be zero!')
        total_blocks = int((total_memory * memory_fraction - peak_memory) / block_mem_size) + prefill_blocks
        npu_mem = total_blocks * block_mem_size
        return npu_mem / 1024 ** 3

    def decode_sim(self, decode_table):
        decode_latency = self.latency_model.decode_model_time_predict(decode_table) + \
            self.latency_model.decode_frame_time_predict(decode_table)
        if decode_latency < 0:
            decode_latency = 0
            logging.debug('Negative decode latency occurs!')
        return decode_latency
    
    def prefill_sim(self, request):
        prefill_latency = self.latency_model.prefill_model_time_predict(request) + \
            self.latency_model.prefill_frame_time_predict(request)
        if prefill_latency < 0:
            prefill_latency = 0
            logging.debug('Negative prefill latency occurs!')
        return prefill_latency

    def recompute(self, decode_table):
        incre_list = []
        block_list = []
        for index, req in enumerate(decode_table):
            if self.block_size == 0:
                raise ValueError('block_size should not be zero!')
            block_list.append(np.ceil((req[1] + req[2]) / self.block_size))
            if (req[1] + req[2]) % self.block_size == 0:
                incre_list.append(index)
        num_available_kv_block = int(self.num_kv_block - sum(block_list))
        if num_available_kv_block >= len(incre_list):
            raise ValueError("Recompute triggered but kv blocks is sufficient.")

        num_alive_block = sum(block_list)
        for req in incre_list[num_available_kv_block:]:
            num_alive_block -= block_list[req]

        if self.num_kv_block == 0:
            raise ValueError('num_kv_block should not be zero!')
        if num_alive_block / self.num_kv_block < self.recompute_ratio:
            needed_block = len(incre_list) - num_available_kv_block
            recompute_req_num, release_kv_block = 1, block_list[-1]
            while release_kv_block < needed_block:
                recompute_req_num += 1
                release_kv_block += block_list[-recompute_req_num]
            recompute_pool = self.recompute_action(decode_table[-recompute_req_num:])
            recompute_time = RECOMPUTE_TIME
            self.logger.info(f'Recompute in is triggered and {recompute_req_num} requests are cleared.')
            return decode_table[:-recompute_req_num], recompute_pool, recompute_time
        else:
            return decode_table, [], 0

    def construct_decode_batch(self, max_batch_size, all_decode_table):
        used_kv_block = 0
        for req in all_decode_table:
            if self.block_size == 0:
                raise ValueError('block_size should not be zero!')
            used_kv_block += np.ceil((req[1] + req[2]) / self.block_size)

        decode_table = []
        for req in all_decode_table:
            if len(decode_table) >= max_batch_size:
                break
            if (req[1] + req[2]) % self.block_size == 0:
                if self.num_kv_block > used_kv_block:
                    decode_table.append(req)
                    used_kv_block += 1
            else:
                decode_table.append(req)
        return decode_table


    def get_npu_mem_size_from_llm_log(self):
        # Get log directory from environment variable with default path
        log_dir = os.getenv("MINDIE_LOG_PATH", "/root/mindie/log/debug/")
        log_files = glob.glob(os.path.join(log_dir, "mindie-llm_*.log"))
        
        if not PathCheck.check_path_full(log_dir):
            raise ValueError('Invalid MINDIE_LOG_PATH environment variable!')
        if not log_files:
            raise FileNotFoundError(f"No log files found in directory: {log_dir}")

        # Sort files by modification time (newest first)
        log_files.sort(key=os.path.getmtime, reverse=True)

        # Regex pattern to match memory value
        pattern = re.compile(r"`Total blocks` needs npu memory\(GB\):\s*(\d+\.\d+)")

        # Search through sorted log files
        for file_path in log_files:
            try:
                with open(file_path, "r") as f:
                    # Read line by line for memory efficiency
                    for line in f:
                        if "`Total blocks` needs npu memory(GB):" in line:
                            match = pattern.search(line)
                            if match:
                                value = float(match.group(1))
                                return float(value)
            except Exception as e:
                raise KeyError(f"Error processing {file_path}: {str(e)}") from e

        self.logger.info("No log entry containing 'Total blocks needs npu memory(GB):' found")
        return None