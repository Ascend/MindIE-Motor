# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import patch

import lib.constant as C
from lib.config_validator import enforce_virtual_inference_log_level


def _engine_with_virtual(enabled: bool) -> dict:
    return {"health_check_config": {"enable_virtual_inference": enabled}}


def test_keeps_virtual_inference_when_log_level_is_error():
    user_config = {
        C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True),
        C.MOTOR_ENGINE_DECODE_CONFIG: _engine_with_virtual(True),
    }
    env_config = {
        C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: C.ASCEND_GLOBAL_LOG_LEVEL_ERROR},
    }

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    assert user_config[C.MOTOR_ENGINE_DECODE_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    mock_logger.warning.assert_not_called()


def test_keeps_virtual_inference_when_log_level_is_integer_error():
    user_config = {C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True)}
    env_config = {C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: 3}}

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    mock_logger.warning.assert_not_called()


def test_keeps_virtual_inference_when_log_level_missing():
    """Unset ASCEND_GLOBAL_LOG_LEVEL defaults to ERROR (3); do not disable virtual inference."""
    user_config = {C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True)}
    env_config = {C.MOTOR_COMMON_ENV: {}}

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    mock_logger.warning.assert_not_called()


def test_disables_virtual_inference_for_non_error_levels():
    for level in ("0", "1", "2", "4", 0, 1, 2, 4):
        user_config = {C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True)}
        env_config = {C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: level}}

        with patch("lib.config_validator.logger") as mock_logger:
            enforce_virtual_inference_log_level(user_config, env_config)

        assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is False, (
            level
        )
        mock_logger.warning.assert_called()
        assert "source: %s" in mock_logger.warning.call_args[0][0]
        assert mock_logger.warning.call_args[0][-1] == C.MOTOR_COMMON_ENV


def test_leaves_disabled_virtual_inference_unchanged():
    user_config = {C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(False)}
    env_config = {C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"}}

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is False
    mock_logger.warning.assert_not_called()


def test_engine_env_overrides_common_log_level():
    user_config = {
        C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True),
        C.MOTOR_ENGINE_DECODE_CONFIG: _engine_with_virtual(True),
    }
    env_config = {
        C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: C.ASCEND_GLOBAL_LOG_LEVEL_ERROR},
        C.MOTOR_ENGINE_PREFILL_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"},
        C.MOTOR_ENGINE_DECODE_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: C.ASCEND_GLOBAL_LOG_LEVEL_ERROR},
    }

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is False
    assert user_config[C.MOTOR_ENGINE_DECODE_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    mock_logger.warning.assert_called_once()
    warning_args = mock_logger.warning.call_args[0]
    assert warning_args[-1] == C.MOTOR_ENGINE_PREFILL_ENV


def test_pd_hybrid_union_role_checked_independently():
    user_config = {
        C.MOTOR_ENGINE_UNION_CONFIG: _engine_with_virtual(True),
        C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True),
    }
    env_config = {
        C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"},
        C.MOTOR_ENGINE_UNION_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: C.ASCEND_GLOBAL_LOG_LEVEL_ERROR},
        C.MOTOR_ENGINE_PREFILL_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"},
    }

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[C.MOTOR_ENGINE_UNION_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is False
    mock_logger.warning.assert_called_once()


def test_decode_role_checked_independently_from_prefill():
    user_config = {
        C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True),
        C.MOTOR_ENGINE_DECODE_CONFIG: _engine_with_virtual(True),
    }
    env_config = {
        C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: C.ASCEND_GLOBAL_LOG_LEVEL_ERROR},
        C.MOTOR_ENGINE_DECODE_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"},
    }

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    assert user_config[C.MOTOR_ENGINE_DECODE_CONFIG]["health_check_config"]["enable_virtual_inference"] is False
    mock_logger.warning.assert_called_once()
    assert mock_logger.warning.call_args[0][-1] == C.MOTOR_ENGINE_DECODE_ENV


def test_ignores_encode_config_even_if_virtual_inference_set():
    encode_key = getattr(C, "MOTOR_ENGINE_ENCODE_CONFIG", "motor_engine_encode_config")
    user_config = {
        encode_key: _engine_with_virtual(True),
        C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True),
    }
    env_config = {C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"}}

    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)

    assert user_config[encode_key]["health_check_config"]["enable_virtual_inference"] is True
    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is False
    mock_logger.warning.assert_called_once()


def test_skips_when_user_config_empty():
    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level({}, {C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"}})
    mock_logger.warning.assert_not_called()


def test_skips_when_no_health_check_config():
    user_config = {C.MOTOR_ENGINE_PREFILL_CONFIG: {"engine_config": {}}}
    env_config = {C.MOTOR_COMMON_ENV: {C.ASCEND_GLOBAL_LOG_LEVEL: "1"}}
    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, env_config)
    mock_logger.warning.assert_not_called()


def test_skips_disable_when_env_config_is_none():
    user_config = {C.MOTOR_ENGINE_PREFILL_CONFIG: _engine_with_virtual(True)}
    with patch("lib.config_validator.logger") as mock_logger:
        enforce_virtual_inference_log_level(user_config, None)
    assert user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]["health_check_config"]["enable_virtual_inference"] is True
    mock_logger.warning.assert_not_called()
