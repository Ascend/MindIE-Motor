# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
from unittest.mock import patch

import pytest

from motor.common.resources.instance import PDRole
from motor.config.node_manager import VLLMStartupAccelerationConfig
from motor.node_manager.core.services.native_engine.startup_acceleration import (
    MAX_STARTUP_PLAN_FILE_SIZE_BYTES,
    VLLM_CACHE_ROOT_ENV,
    VLLM_DISABLE_COMPILE_CACHE_ENV,
    VLLM_ENABLE_STARTUP_PLAN_ENV,
    build_vllm_graph_reuse_engine_overrides,
    build_vllm_startup_environment,
    inspect_graph_reuse_cache_root,
    inspect_startup_plan_profiles,
    resolve_vllm_cache_root,
)


@pytest.mark.parametrize(
    ("startup_plan", "graph_reuse"),
    [(False, False), (True, False), (False, True)],
)
def test_build_vllm_startup_environment(startup_plan, graph_reuse):
    config = VLLMStartupAccelerationConfig(startup_plan, graph_reuse, "/mnt/cache")
    environment = build_vllm_startup_environment(config, {})
    assert environment[VLLM_CACHE_ROOT_ENV] == "/mnt/cache"
    assert environment[VLLM_ENABLE_STARTUP_PLAN_ENV] == str(int(startup_plan))
    assert (VLLM_DISABLE_COMPILE_CACHE_ENV in environment) is graph_reuse
    assert "VLLM_USE_AOT_COMPILE" not in environment
    assert "VLLM_FORCE_AOT_LOAD" not in environment


def test_build_vllm_startup_environment_warns_only_for_conflicting_values(caplog):
    config = VLLMStartupAccelerationConfig(False, True, "/config/cache")
    inherited = {
        VLLM_CACHE_ROOT_ENV: "/env/cache",
        VLLM_ENABLE_STARTUP_PLAN_ENV: "1",
        VLLM_DISABLE_COMPILE_CACHE_ENV: "1",
    }
    build_vllm_startup_environment(config, inherited)
    assert caplog.text.count("Motor overrides environment") == 3

    caplog.clear()
    build_vllm_startup_environment(config, {VLLM_ENABLE_STARTUP_PLAN_ENV: "0"})
    assert "overrides environment" not in caplog.text


@pytest.mark.parametrize(
    ("configured", "environment", "expected"),
    [
        ("/config/cache", {VLLM_CACHE_ROOT_ENV: "/env/cache"}, "/config/cache"),
        ("", {VLLM_CACHE_ROOT_ENV: "/env/cache"}, "/env/cache"),
        ("", {"XDG_CACHE_HOME": "/xdg/cache"}, "/xdg/cache/vllm"),
        ("", {"HOME": "/home/motor"}, "/home/motor/.cache/vllm"),
    ],
)
def test_resolve_vllm_cache_root_priority(configured, environment, expected):
    assert resolve_vllm_cache_root(VLLMStartupAccelerationConfig(cache_root=configured), environment) == expected


@pytest.mark.parametrize(
    ("enabled", "role", "expected_mode"),
    [
        (False, PDRole.ROLE_P, None),
        (True, PDRole.ROLE_P, "FULL"),
        (True, PDRole.ROLE_D, "FULL_DECODE_ONLY"),
        (True, PDRole.ROLE_U, "FULL"),
    ],
)
def test_graph_reuse_engine_overrides(enabled, role, expected_mode):
    overrides = build_vllm_graph_reuse_engine_overrides(VLLMStartupAccelerationConfig(enable_graph_reuse=enabled), role)
    ascend = overrides["additional_config"]["ascend_compilation_config"]
    assert ascend["enable_npugraph_ex"] is enabled
    if enabled:
        assert overrides["enforce_eager"] is False
        assert overrides["compilation_config"]["cudagraph_mode"] == expected_mode
    else:
        assert ascend["enable_static_kernel"] is False
        assert "enforce_eager" not in overrides


def test_inspect_graph_reuse_cache_root_accepts_a_creatable_path(tmp_path):
    inspect_graph_reuse_cache_root(str(tmp_path / "new-cache"))


@pytest.mark.parametrize("inspector", [inspect_graph_reuse_cache_root, inspect_startup_plan_profiles])
def test_cache_preflight_rejects_inaccessible_root(inspector, tmp_path):
    with patch(
        "motor.node_manager.core.services.native_engine.startup_acceleration.os.access",
        return_value=False,
    ):
        with pytest.raises(ValueError, match="not accessible"):
            inspector(str(tmp_path))


def test_inspect_startup_plan_profiles_counts_candidates(tmp_path):
    plan_directory = tmp_path / "startup_plan"
    plan_directory.mkdir()
    (plan_directory / "startup_plan_valid.json").write_text(json.dumps({"schema": 1}), encoding="utf-8")
    (plan_directory / "startup_plan_malformed.json").write_text("{", encoding="utf-8")
    (plan_directory / "startup_plan_oversized.json").write_bytes(b" " * (MAX_STARTUP_PLAN_FILE_SIZE_BYTES + 1))

    assert inspect_startup_plan_profiles(str(tmp_path)) == (3, 1, 2)


def test_inspect_startup_plan_profiles_treats_missing_cache_as_cold_start(tmp_path):
    assert inspect_startup_plan_profiles(str(tmp_path / "new-cache")) == (0, 0, 0)


def test_inspect_startup_plan_profiles_rejects_non_string_cache_root():
    with pytest.raises(ValueError, match="cache_root must be a non-empty string"):
        inspect_startup_plan_profiles(123)  # type: ignore[arg-type]
