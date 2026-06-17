#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.constants import CHAT_COMPLETION_PREFIX, COMPLETION_PREFIX

# log dir permission
MOTOR_CUSTOM_ZMQ_PRIVILEGE = 0o640
MOTOR_CUSTOM_ZMQ_DIR_PRIVILEGE = 0o750
MUSK_PRIVILEGE = 0o777

# logger default config
LOG_MAX_LINE_LENGTH = 1023
LOG_SIMPLE_FORMAT = '[%(levelname)s]     %(asctime)s.%(msecs)06d %(process)d   %(filename)s:%(lineno)d     %(message)s'
LOG_DATE_FORMAT = '%Y/%m/%d %H:%M:%S'
LOG_BACKUP_FORMAT = '%Y-%m-%dT%H-%M-%S.%f'
LOG_BACKUP_PATTERN = '\\d{4}-\\d{2}-\\d{2}T\\d{2}-\\d{2}-\\d{2}\\.\\d{3}'
LOG_DEFAULT_FILE = "./engine_server_log/engine_server.log"
LOG_DEFAULT_FILE_PATH = "./engine_server_log/"
LOG_DEFAULT_FILE_NAME = "engine_server.log"
LOG_DEFAULT_BACKUP_COUNT = 30
LOG_DEFAULT_MAX_BYTES = 1024 * 1024 * 20

# valid boundary value
MIN_RANK_SIZE = 0
MAX_RANK_SIZE = 4095
MAX_FILE_NUMS = 4096
MIN_DEVICE_NUM = 1
MAX_DEVICE_NUM = 4096
MAX_SIZE = 1024 * 1024
MIN_SIZE = 0

# PD role
PREFILL_ROLE = "prefill"
DECODE_ROLE = "decode"
UNION_ROLE = "union"

# kv transfer config keys
KV_TRANSFER_CONFIG = "kv_transfer_config"
KV_ROLE = "kv_role"
KV_PORT = "kv_port"
KV_PRODUCER = "kv_producer"
KV_CONSUMER = "kv_consumer"
KV_CONNECTOR_EXTRA_CONFIG = "kv_connector_extra_config"
KV_PREFILL = "prefill"
KV_DECODE = "decode"
KV_CONNECTOR = "kv_connector"
MULTI_CONNECTOR = "MultiConnector"
MOON_CAKE_STORE_V1 = "MooncakeConnectorStoreV1"
ASCEND_STORE_CONNECTOR = "AscendStoreConnector"
MOON_CAKE_RPC_PORT = "mooncake_rpc_port"
LOOKUP_RPC_PORT = "lookup_rpc_port"
CONNECTORS = "connectors"

# parallel config keys
DP_SIZE = "dp_size"
TP_SIZE = "tp_size"
PP_SIZE = "pp_size"

# engine config
ENGINE_ID = "engine_id"

# server status
INIT_STATUS = "initial"
ABNORMAL_STATUS = "abnormal"
NORMAL_STATUS = "normal"

# response keys
STATUS_KEY = "status"

# content type
TEXT_PLAIN = "text/plain"
APPLICATION_JSON = "application/json"
TEXT_EVENT_STREAM = "text/event-stream"

# http headers
CONTENT_TYPE = "content-type"
CONTENT_LENGTH = "content-length"
TRANSFER_ENCODING = "transfer-encoding"
CHUNKED_ENCODING = "chunked"

# json field names
JSON_ID_FIELD = "id"

# vllm related constants (shared with coordinator via motor.common.constants)

# vllm api paths
COMPLETIONS_PATH = "/v1/completions"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

# vllm stream data format
DATA_PREFIX = "data: "
DATA_DONE = "data: [DONE]"

# mgmt interface name
METRICS_INTERFACE = "/metrics"
STATUS_INTERFACE = "/status"

# service type
METRICS_SERVICE = "metrics_service"
HEALTH_SERVICE = "health_service"

DISAGGREGATION_MODE = "disaggregation-mode"
