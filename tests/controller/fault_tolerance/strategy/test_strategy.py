# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Fault tolerance strategy test cases.

Test cases are organized according to the following logical blocks:
1. Strategy base class functionality
2. General healthy strategy (L0)
3. Level 1 strategy testing
4. Level 2 strategy testing
5. Level 3 strategy testing (calls L1 strategy logic)
6. Level 4 strategy testing
7. Level 5 strategy testing (calls L4 strategy logic)
8. Level 6 strategy testing (calls L4 strategy logic)
9. Strategy map generation and usage
10. Configuration switch testing
"""

# pylint: disable=redefined-outer-name

import pytest
from unittest.mock import Mock, patch

from motor.controller.fault_tolerance.fault_types import FaultLevel
from motor.controller.fault_tolerance.strategy.strategy import (
    StrategyBase,
    healthy_strategy,
    level2_strategy,
    level3_strategy,
    level4_strategy,
    level5_strategy,
    level6_strategy,
    generate_strategy_map,
)
from motor.controller.fault_tolerance.strategy.token_reinference import TokenReinferenceStrategy
from motor.controller.fault_tolerance.strategy.scale_p2d import ScaleP2DStrategy
from motor.config.controller import ControllerConfig


# Fixtures
@pytest.fixture
def mock_config():
    """Fixture for creating a mock config with strategies enabled"""
    config = ControllerConfig()
    config.fault_tolerance_config.enable_scale_p2d = True
    config.fault_tolerance_config.enable_token_reinference = True
    return config


@pytest.fixture
def mock_config_scale_p2d_disabled():
    """Fixture for creating a mock config with scale_p2d disabled"""
    config = ControllerConfig()
    config.fault_tolerance_config.enable_scale_p2d = False
    config.fault_tolerance_config.enable_token_reinference = True
    return config


@pytest.fixture
def mock_config_token_reinference_disabled():
    """Fixture for creating a mock config with token reinference disabled"""
    config = ControllerConfig()
    config.fault_tolerance_config.enable_scale_p2d = True
    config.fault_tolerance_config.enable_token_reinference = False
    return config


@pytest.fixture
def mock_instance_manager():
    """Fixture for mocking InstanceManager"""
    with patch("motor.controller.core.instance_manager.InstanceManager") as mock:
        yield mock


@pytest.fixture
def decode_instance():
    """Fixture for creating a decode role instance"""
    instance = Mock()
    instance.role = "decode"
    return instance


@pytest.fixture
def encode_instance():
    """Fixture for creating an encode role instance"""
    instance = Mock()
    instance.role = "encode"
    return instance


# StrategyBase Tests
def test_strategy_base_execute_raises_not_implemented_error():
    """StrategyBase.execute should raise NotImplementedError when not implemented"""

    class ConcreteStrategy(StrategyBase):
        def execute(self, instance_id):
            raise NotImplementedError("execute method not implemented")

        def stop(self):
            raise NotImplementedError("stop method not implemented")

    strategy = ConcreteStrategy()

    with pytest.raises(NotImplementedError, match="execute method not implemented"):
        strategy.execute(instance_id=None)  # type: ignore[arg-type]


# L0 Strategy Tests
@pytest.mark.parametrize("fault_code", [0x0000, 0x00F1FEF5, 0x08520003, 0x12345678])
def test_general_l0_strategy_returns_none_for_any_fault_code(fault_code, mock_config):
    """L0 strategy should always return None since it represents healthy state"""
    assert healthy_strategy(fault_code, 1, mock_config) is None


# L2 Strategy Tests
@pytest.mark.parametrize(
    "fault_code,expected",
    [
        (0x00F1FEF5, TokenReinferenceStrategy),
        (0x08520003, TokenReinferenceStrategy),
        (0, None),
        (0x0000, None),
    ],
)
def test_level2_strategy_returns_correct_class(fault_code, expected, mock_config):
    """L2 strategy should return TokenReinferenceStrategy for known codes, None otherwise"""
    assert level2_strategy(fault_code, 1, mock_config) is expected


# L3 Strategy Tests
@pytest.mark.parametrize("fault_code", [0x0000, 0x00F1FEF5, 0x08520003, 0x12345678])
def test_level3_strategy_returns_none_for_any_fault_code(fault_code, mock_config):
    """L3 strategy should always return None since it calls L1 strategy logic"""
    assert level3_strategy(fault_code, 1, mock_config) is None


# Level4 Strategy Tests
def test_level4_strategy_returns_scale_p2d_for_decode_role(mock_instance_manager, decode_instance, mock_config):
    """When instance role is decode, level4 strategy should return ScaleP2DStrategy"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    result = level4_strategy(0x0000, 1, mock_config)

    assert result is ScaleP2DStrategy


def test_level4_strategy_returns_none_for_non_decode_role(mock_instance_manager, encode_instance, mock_config):
    """When instance role is not decode, level4 strategy should return None"""
    mock_instance_manager.return_value.get_instance.return_value = encode_instance

    result = level4_strategy(0x0000, 1, mock_config)

    assert result is None


def test_level4_strategy_returns_none_when_instance_is_none(mock_instance_manager, mock_config):
    """When instance is None, level4 strategy should return None"""
    mock_instance_manager.return_value.get_instance.return_value = None

    result = level4_strategy(0x0000, 1, mock_config)

    assert result is None


# Strategy Map Generation Tests
def test_generate_strategy_map_contains_expected_levels():
    """Strategy map should contain all expected fault levels"""

    strategies = generate_strategy_map()

    expected_keys = {
        FaultLevel.HEALTHY,
        FaultLevel.L1,
        FaultLevel.L2,
        FaultLevel.L3,
        FaultLevel.L4,
        FaultLevel.L5,
        FaultLevel.L6,
    }
    assert set(strategies.keys()) == expected_keys


@pytest.mark.parametrize("level", [0, 1, 3])
def test_strategy_map_level_returns_none_for_l0_l1_and_l3(level, mock_config):
    """L0, L1 and L3 strategies should always return None (L3 calls L1 strategy logic)"""
    strategies = generate_strategy_map()
    strategy_func = strategies[level]

    assert callable(strategy_func)
    assert strategy_func(0x0000, 1, mock_config) is None


def test_strategy_map_l2_returns_token_reinference_for_known_codes(mock_config):
    """L2 strategy should return TokenReinferenceStrategy for known error codes"""
    strategies = generate_strategy_map()
    l2_func = strategies[2]

    assert callable(l2_func)
    assert l2_func(0x00F1FEF5, 1, mock_config) is TokenReinferenceStrategy
    assert l2_func(0x08520003, 1, mock_config) is TokenReinferenceStrategy
    assert l2_func(0, 1, mock_config) is None


def test_strategy_map_l4_returns_scale_p2d_for_decode_role(mock_instance_manager, decode_instance, mock_config):
    """L4 strategy should return ScaleP2DStrategy when instance role is decode"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    strategies = generate_strategy_map()
    strategy_func = strategies[4]

    assert callable(strategy_func)
    assert strategy_func(0x0000, 1, mock_config) is ScaleP2DStrategy


def test_strategy_map_l5_returns_scale_p2d_for_decode_role(mock_instance_manager, decode_instance, mock_config):
    """L5 strategy should return ScaleP2DStrategy when instance role is decode (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    strategies = generate_strategy_map()
    strategy_func = strategies[5]

    assert callable(strategy_func)
    assert strategy_func(0x0000, 1, mock_config) is ScaleP2DStrategy


def test_strategy_map_l6_returns_scale_p2d_for_decode_role(mock_instance_manager, decode_instance, mock_config):
    """L6 strategy should return ScaleP2DStrategy when instance role is decode (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    strategies = generate_strategy_map()
    strategy_func = strategies[6]

    assert callable(strategy_func)
    assert strategy_func(0x0000, 1, mock_config) is ScaleP2DStrategy


def test_level2_strategy_respects_config_switch(mock_config_token_reinference_disabled):
    """L2 strategy should return None when token reinference is disabled in config"""
    result = level2_strategy(0x00F1FEF5, 1, mock_config_token_reinference_disabled)
    assert result is None

    result = level2_strategy(0x08520003, 1, mock_config_token_reinference_disabled)
    assert result is None


def test_level4_strategy_respects_config_switch(mock_instance_manager, decode_instance, mock_config_scale_p2d_disabled):
    """Level4 strategy should return None when scale_p2d is disabled in config"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    result = level4_strategy(0x0000, 1, mock_config_scale_p2d_disabled)
    assert result is None


# Level5 Strategy Tests
def test_level5_strategy_returns_scale_p2d_for_decode_role(mock_instance_manager, decode_instance, mock_config):
    """L5 strategy should return ScaleP2DStrategy when instance role is decode (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    result = level5_strategy(0x0000, 1, mock_config)

    assert result is ScaleP2DStrategy


def test_level5_strategy_returns_none_for_non_decode_role(mock_instance_manager, encode_instance, mock_config):
    """L5 strategy should return None when instance role is not decode (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = encode_instance

    result = level5_strategy(0x0000, 1, mock_config)

    assert result is None


def test_level5_strategy_returns_none_when_instance_is_none(mock_instance_manager, mock_config):
    """L5 strategy should return None when instance is None (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = None

    result = level5_strategy(0x0000, 1, mock_config)

    assert result is None


def test_level5_strategy_respects_config_switch(mock_instance_manager, decode_instance, mock_config_scale_p2d_disabled):
    """L5 strategy should return None when scale_p2d is disabled in config (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    result = level5_strategy(0x0000, 1, mock_config_scale_p2d_disabled)
    assert result is None


# Level6 Strategy Tests
def test_level6_strategy_returns_scale_p2d_for_decode_role(mock_instance_manager, decode_instance, mock_config):
    """L6 strategy should return ScaleP2DStrategy when instance role is decode (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    result = level6_strategy(0x0000, 1, mock_config)

    assert result is ScaleP2DStrategy


def test_level6_strategy_returns_none_for_non_decode_role(mock_instance_manager, encode_instance, mock_config):
    """L6 strategy should return None when instance role is not decode (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = encode_instance

    result = level6_strategy(0x0000, 1, mock_config)

    assert result is None


def test_level6_strategy_returns_none_when_instance_is_none(mock_instance_manager, mock_config):
    """L6 strategy should return None when instance is None (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = None

    result = level6_strategy(0x0000, 1, mock_config)

    assert result is None


def test_level6_strategy_respects_config_switch(mock_instance_manager, decode_instance, mock_config_scale_p2d_disabled):
    """L6 strategy should return None when scale_p2d is disabled in config (calls L4 logic)"""
    mock_instance_manager.return_value.get_instance.return_value = decode_instance

    result = level6_strategy(0x0000, 1, mock_config_scale_p2d_disabled)
    assert result is None
