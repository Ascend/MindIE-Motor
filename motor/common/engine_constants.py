# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

MUSK_PRIVILEGE = 0o777

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
KV_CONNECTOR_MODULE_PATH = "kv_connector_module_path"
UCM_CONNECTOR = "UCMConnector"
KV_BOTH = "kv_both"

# parallel config keys
DP_SIZE = "dp_size"
TP_SIZE = "tp_size"
PP_SIZE = "pp_size"

# engine config
ENGINE_ID = "engine_id"

DISAGGREGATION_MODE = "disaggregation-mode"
