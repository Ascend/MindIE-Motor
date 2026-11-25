# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Normalize runtime settings derived from environment configuration."""

import lib.constant as C
from lib.utils import logger


def apply_decode_timeout(user_config: dict, env_config: dict) -> None:
    """Align Coordinator's timeout with the selected engine abort timeout."""
    common_env = env_config.get(C.MOTOR_COMMON_ENV, {})
    prefill_env = env_config.get(C.MOTOR_ENGINE_PREFILL_ENV, {})
    if not isinstance(common_env, dict) or not isinstance(prefill_env, dict):
        return
    if C.MOONCAKE_ABORT_TIMEOUT_ENV in prefill_env:
        source_section = C.MOTOR_ENGINE_PREFILL_ENV
        raw_timeout = prefill_env[C.MOONCAKE_ABORT_TIMEOUT_ENV]
    elif C.MOONCAKE_ABORT_TIMEOUT_ENV in common_env:
        source_section = C.MOTOR_COMMON_ENV
        raw_timeout = common_env[C.MOONCAKE_ABORT_TIMEOUT_ENV]
    else:
        return

    if isinstance(raw_timeout, bool):
        raise ValueError(f"{source_section}.{C.MOONCAKE_ABORT_TIMEOUT_ENV} must be a positive integer")  # noqa: TRY004 -- preserve public JSON validation errors
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{source_section}.{C.MOONCAKE_ABORT_TIMEOUT_ENV} must be a positive integer") from error
    if timeout <= 0 or str(raw_timeout).strip() != str(timeout):
        raise ValueError(f"{source_section}.{C.MOONCAKE_ABORT_TIMEOUT_ENV} must be a positive integer")

    coordinator_config = user_config.setdefault(C.MOTOR_COORDINATOR_CONFIG, {})
    exception_config = coordinator_config.setdefault(C.EXCEPTION_CONFIG, {})
    exception_config[C.DECODE_STREAM_TIMEOUT_FIELD] = timeout
    logger.info(
        "Set %s.%s.%s to %s from %s.%s",
        C.MOTOR_COORDINATOR_CONFIG,
        C.EXCEPTION_CONFIG,
        C.DECODE_STREAM_TIMEOUT_FIELD,
        timeout,
        source_section,
        C.MOONCAKE_ABORT_TIMEOUT_ENV,
    )
