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

import pytest

from motor.config.config_utils import resolve_request_model_map
from motor.config.coordinator import CoordinatorConfig


def _load(tmp_path, options, engines=None, nested=None):
    data = {"motor_coordinator_config": {}, "kv_conductor_config": options, **(engines or {})}
    if nested is not None:
        data["motor_coordinator_config"]["scheduler_config"] = {"kv_conductor_config": nested}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return CoordinatorConfig.from_json(str(path))


@pytest.mark.parametrize("role", ["prefill", "union"])
@pytest.mark.parametrize("render_enable,expected_render", [(None, True), (False, False)])
def test_v1_loads_mode_and_derives_model_names_without_duplicate_config(tmp_path, role, render_enable, expected_render):
    engine = {
        "engine_type": "vllm",
        "engine_config": {
            "model": "/weights/base",
            "served-model-name": ["base", "alias"],
            "max_model_len": 4096,
            "enable-lora": True,
            "lora-modules": [
                "sql@v1=/weights/sql",
                {"name": "chat", "path": "/weights/chat"},
                '{"name":"json-lora","path":"/weights/json"}',
            ],
        },
    }
    coordinator = {"scheduler_config": {"prefill_scheduler_type": "kv_cache_affinity"}}
    if render_enable is not None:
        coordinator["render_config"] = {"enable": render_enable}
    config = _load(
        tmp_path,
        {"identity_mode": "v1"},
        {f"motor_engine_{role}_config": engine, "motor_coordinator_config": coordinator},
    )
    assert config.scheduler_config.kv_conductor_config.identity_mode == "v1"
    assert config.render_config.enable is expected_render
    assert "identity_main_group" not in config.to_dict()["scheduler_config"]["kv_conductor_config"]
    assert config.request_model_map == {
        "base": {"kind": "base"},
        "alias": {"kind": "base"},
        "sql@v1": {"kind": "lora", "name": "sql@v1"},
        "chat": {"kind": "lora", "name": "chat"},
        "json-lora": {"kind": "lora", "name": "json-lora"},
    }
    assert "request_model_map" not in config.to_dict()
    assert "runtime_guard_enabled" not in config.to_dict()


def test_model_map_uses_vllm_path_fallback_and_ignores_disabled_lora_and_other_engines():
    assert resolve_request_model_map(
        {
            "motor_engine_prefill_config": {
                "engine_type": "vllm",
                "engine_config": {
                    "model": "/weights/base",
                    "enable_lora": False,
                    "lora_modules": ["unused=/weights/unused"],
                },
            },
            "motor_engine_decode_config": {"engine_type": "sglang", "engine_config": {"model": "sglang-model"}},
        }
    ) == {"/weights/base": {"kind": "base"}}


def test_invalid_identity_mode_fails_configuration_loading(tmp_path):
    with pytest.raises(ValueError, match="identity_mode"):
        _load(tmp_path, {"identity_mode": "other"})


@pytest.mark.parametrize("nested_mode", ["legacy", ""])
def test_conflicting_identity_modes_stop_configuration_loading(tmp_path, nested_mode):
    with pytest.raises(ValueError, match="Conflicting"):
        _load(tmp_path, {"identity_mode": "v1"}, nested={"identity_mode": nested_mode})


@pytest.mark.parametrize("content", ["", "{", "{}"])
def test_empty_or_unreadable_config_uses_default_identity_mode(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")
    config = CoordinatorConfig.from_json(str(path))
    assert config.scheduler_config.kv_conductor_config.identity_mode == "legacy"
    assert config.request_model_map == {}


@pytest.mark.parametrize(
    "module,reason",
    [
        ("/weights/adapter", "name=path"),
        ("sql=/weights/sql=extra", "name=path"),
        ("sql=/weights/sql,extra", "name=path"),
        (" =/weights/sql", "nonempty name"),
        ("sql= ", "nonempty path"),
        ({"path": "/weights/sql"}, "nonempty name"),
        ({"name": "sql"}, "nonempty path"),
        ({"name": "sql", "path": 1}, "nonempty path"),
        (' {"name": "sql",}', "invalid JSON: Expecting property name"),
    ],
)
def test_invalid_lora_configuration_does_not_create_a_base_identity(module, reason):
    with pytest.raises(ValueError, match=rf"motor_engine_prefill_config\.engine_config\.lora_modules\[1\].*{reason}"):
        resolve_request_model_map(
            {
                "motor_engine_prefill_config": {
                    "engine_config": {
                        "model": "base",
                        "enable_lora": True,
                        "lora_modules": ["valid=/weights/valid", module],
                    }
                }
            }
        )


def test_json_lora_preserves_declared_name_and_allows_engine_options():
    engine = {
        "model": "base",
        "enable_lora": True,
        "lora_modules": ['  {"name": " sql ", "path": "/weights/v=1,sql", "base_model_name": "base"}'],
    }
    model_map = resolve_request_model_map({"motor_engine_union_config": {"engine_config": engine}})
    assert model_map[" sql "] == {"kind": "lora", "name": " sql "}


def test_model_alias_cannot_mean_both_base_and_lora():
    with pytest.raises(ValueError, match="conflict"):
        resolve_request_model_map(
            {
                "motor_engine_prefill_config": {"engine_config": {"model": "adapter"}},
                "motor_engine_decode_config": {
                    "engine_config": {
                        "model": "base",
                        "enable_lora": True,
                        "lora_modules": ["adapter=/weights/adapter"],
                    }
                },
            }
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"served_model_name": {"adapter": "base"}},
        {"served_model_name": ["base", 1]},
        {"enable_lora": True, "lora_modules": "sql=/weights/sql"},
        {"enable_lora": True, "lora_modules": ["base=/weights/sql"]},
    ],
)
def test_invalid_engine_model_fields_cannot_grant_a_base_identity(fields):
    with pytest.raises(ValueError):
        resolve_request_model_map({"motor_engine_prefill_config": {"engine_config": {"model": "base", **fields}}})


def test_engine_model_mapping_does_not_require_an_explicit_coordinator_section(tmp_path):
    path = tmp_path / "engine-only.json"
    path.write_text(
        json.dumps(
            {
                "kv_conductor_config": {"identity_mode": "v1"},
                "motor_engine_prefill_config": {
                    "engine_config": {
                        "served-model-name": ["base", "alias"],
                        "enable-lora": True,
                        "lora-modules": ["sql=/weights/sql"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert CoordinatorConfig.from_json(str(path)).request_model_map == {
        "base": {"kind": "base"},
        "alias": {"kind": "base"},
        "sql": {"kind": "lora", "name": "sql"},
    }
