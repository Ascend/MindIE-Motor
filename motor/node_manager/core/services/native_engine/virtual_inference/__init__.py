# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.node_manager.core.services.native_engine.virtual_inference.capabilities import (
    ASCEND_GLOBAL_LOG_LEVEL_ENV,
    ASCEND_GLOBAL_LOG_LEVEL_ERROR,
    is_error_ascend_global_log_level,
    should_enable_vllm_virtual_inference,
)
from motor.node_manager.core.services.native_engine.virtual_inference.requesters import (
    VIRTUAL_REQUEST_ID_MARKER,
    VllmCompletionsRequester,
    generate_request_id,
)
from motor.node_manager.core.services.native_engine.virtual_inference.spec import TargetIdentity, VirtualInferenceSpec
from motor.node_manager.core.services.native_engine.virtual_inference.worker import VirtualInferenceWorker

__all__ = [
    "ASCEND_GLOBAL_LOG_LEVEL_ENV",
    "ASCEND_GLOBAL_LOG_LEVEL_ERROR",
    "VIRTUAL_REQUEST_ID_MARKER",
    "TargetIdentity",
    "VirtualInferenceSpec",
    "VirtualInferenceWorker",
    "VllmCompletionsRequester",
    "generate_request_id",
    "is_error_ascend_global_log_level",
    "should_enable_vllm_virtual_inference",
]
