# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.logger import get_logger

logger = get_logger(__name__)

ASCEND_GLOBAL_LOG_LEVEL_ENV = "ASCEND_GLOBAL_LOG_LEVEL"
ASCEND_GLOBAL_LOG_LEVEL_ERROR = "3"


def is_error_ascend_global_log_level(raw_value) -> bool:
    """Return True when ASCEND_GLOBAL_LOG_LEVEL allows Motor vLLM virtual inference (unset/empty/\"3\")."""
    if raw_value is None:
        return True
    text = str(raw_value).strip()
    if not text:
        return True
    return text == ASCEND_GLOBAL_LOG_LEVEL_ERROR


def should_enable_vllm_virtual_inference(
    *,
    enable_virtual_inference: bool,
    dp_rank: int,
    headless: bool,
    npu_usage_threshold: int,
) -> bool:
    """Return True when Motor should run virtual inference for this vLLM endpoint (DP0, non-headless)."""
    if not enable_virtual_inference:
        return False
    if dp_rank != 0:
        logger.info(
            "Virtual inference is disabled on DP rank %s (only DP rank 0 performs virtual inference)",
            dp_rank,
        )
        return False
    if headless:
        logger.info("Virtual inference is disabled for headless endpoint")
        return False
    if npu_usage_threshold <= 0 or npu_usage_threshold > 100:
        logger.info(
            "Virtual inference is disabled because npu_usage_threshold %s is abnormal",
            npu_usage_threshold,
        )
        return False
    return True
