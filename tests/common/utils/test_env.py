# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for motor.common.utils.env (_Environment / Env)."""

import pytest

from motor.common.utils.env import Env, _Environment

COORDINATOR_DEFAULT = "mindie-motor-coordinator-service.mindie-motor.svc.cluster.local"
CONTROLLER_DEFAULT = "mindie-motor-controller-service.mindie-motor.svc.cluster.local"

# (attribute, environment variable, default when unset); motor_restart_local_service is boolean and tested separately
PROPERTIES = [
    ("job_name", "JOB_NAME", None),
    ("config_path", "CONFIG_PATH", None),
    ("hccl_path", "HCCL_PATH", None),
    ("ranktable_path", "RANKTABLE_PATH", None),
    ("user_config_path", "USER_CONFIG_PATH", None),
    ("role", "ROLE", None),
    ("index", "INDEX", None),
    ("pod_ip", "POD_IP", None),
    ("coordinator_service", "COORDINATOR_SERVICE", COORDINATOR_DEFAULT),
    ("coordinator_infer_service", "COORDINATOR_INFER_SERVICE", COORDINATOR_DEFAULT),
    ("coordinator_obs_service", "COORDINATOR_OBS_SERVICE", COORDINATOR_DEFAULT),
    ("controller_service", "CONTROLLER_SERVICE", CONTROLLER_DEFAULT),
    ("conductor_service", "KV_CONDUCTOR_SERVICE", ""),
    ("kv_store_backend", "KV_STORE_BACKEND", ""),
    ("kv_cache_store_port", "KV_CACHE_STORE_PORT", ""),
    ("kvs_master_service", "KVS_MASTER_SERVICE", ""),
    ("mmc_local_config_path", "MMC_LOCAL_CONFIG_PATH", ""),
    ("mooncake_config_path", "MOONCAKE_CONFIG_PATH", ""),
    ("mmc_local_service_mode", "MMC_LOCAL_SERVICE_MODE", ""),
    ("mmc_config_store_url", "MMC_CONFIG_STORE_URL", ""),
]

# These attributes fall back to COORDINATOR_SERVICE when their own env var is unset
FALLBACK_ATTRS = {"coordinator_infer_service", "coordinator_obs_service"}

ATTR_TO_ENV = {attr: env_var for attr, env_var, _ in PROPERTIES}


@pytest.mark.parametrize(("attr", "env_var", "_default"), PROPERTIES)
def test_env_property_when_variable_set(attr, env_var, _default, monkeypatch):
    monkeypatch.setenv(env_var, "test-value")
    assert getattr(_Environment(), attr) == "test-value"


@pytest.mark.parametrize(("attr", "env_var", "default"), PROPERTIES)
def test_env_property_when_variable_unset(attr, env_var, default, monkeypatch):
    monkeypatch.delenv(env_var, raising=False)
    if attr in FALLBACK_ATTRS:
        monkeypatch.delenv("COORDINATOR_SERVICE", raising=False)
    assert getattr(_Environment(), attr) == default


@pytest.mark.parametrize("attr", sorted(FALLBACK_ATTRS))
def test_coordinator_service_fallback_used_when_own_variable_unset(attr, monkeypatch):
    monkeypatch.delenv(ATTR_TO_ENV[attr], raising=False)
    monkeypatch.setenv("COORDINATOR_SERVICE", "coordinator.custom.svc")
    assert getattr(_Environment(), attr) == "coordinator.custom.svc"


# The attribute-specific variable takes precedence when both variables are set; lock the fallback priority direction
@pytest.mark.parametrize("attr", sorted(FALLBACK_ATTRS))
def test_fallback_prefers_own_variable_over_coordinator_service(attr, monkeypatch):
    monkeypatch.setenv(ATTR_TO_ENV[attr], "own.svc")
    monkeypatch.setenv("COORDINATOR_SERVICE", "fallback.svc")
    assert getattr(_Environment(), attr) == "own.svc"


def test_motor_restart_local_service_defaults_to_true(monkeypatch):
    monkeypatch.delenv("MOTOR_RESTART_LOCAL_SERVICE", raising=False)
    assert _Environment().motor_restart_local_service is True


def test_motor_restart_local_service_enabled_when_set_to_one(monkeypatch):
    monkeypatch.setenv("MOTOR_RESTART_LOCAL_SERVICE", "1")
    assert _Environment().motor_restart_local_service is True


def test_motor_restart_local_service_disabled_when_set_to_other_value(monkeypatch):
    monkeypatch.setenv("MOTOR_RESTART_LOCAL_SERVICE", "0")
    assert _Environment().motor_restart_local_service is False


def test_module_env_singleton_is_usable(monkeypatch):
    monkeypatch.delenv("KV_CONDUCTOR_SERVICE", raising=False)
    assert isinstance(Env, _Environment)
    assert Env.coordinator_service
    assert Env.controller_service
    assert Env.conductor_service == ""
    for attr, _, _ in PROPERTIES:
        getattr(Env, attr)
    assert Env.motor_restart_local_service is not None
