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
import sys
from copy import deepcopy
from pathlib import PurePosixPath

import pytest

from motor.node_manager.core.services.native_engine.backends.vllm import log_cli_config as paths
from motor.node_manager.core.services.native_engine.backends.vllm.log_cli_config import VllmLogCliCapabilities

ROOT = PurePosixPath("/logs/unit/vllm")
ALL_CAPABILITIES = VllmLogCliCapabilities(runtime_paths=True, watchdog=True)


@pytest.mark.parametrize(
    "vllm,ascend,runtime,watchdog",
    [
        ("0.30.0", "0.30.0", False, False),
        ("0.33.0+empty", "0.33.0", True, True),
        ("0.33.0", "0.19.1rc2.dev3543", False, True),
        ("0.30.0", "0.33.0", True, False),
        ("0.33.0rc1", "0.33.0.dev100", False, False),
        ("0.34.0rc1", "0.34.0.dev1", True, True),
        ("0.33.0.post1", None, False, True),
        ("unversioned", "0.33.0", True, False),
        (None, "", False, False),
    ],
)
def test_independent_package_rules_follow_pep440(vllm, ascend, runtime, watchdog):
    assert paths.resolve_log_cli_capabilities({"vllm": vllm, "vllm-ascend": ascend}) == VllmLogCliCapabilities(
        runtime_paths=runtime, watchdog=watchdog
    )


@pytest.mark.parametrize("failure", [paths.metadata.PackageNotFoundError, OSError, UnicodeError])
def test_metadata_failure_preserves_the_other_package_without_importing_engines(monkeypatch, failure):
    def read_version(name):
        if name == "vllm":
            raise failure("unavailable metadata")
        return "0.33.0+custom"

    monkeypatch.setitem(sys.modules, "vllm", None)
    monkeypatch.setitem(sys.modules, "vllm_ascend", None)
    monkeypatch.setattr(paths.metadata, "version", read_version)
    versions = paths.read_engine_versions()
    assert versions == {"vllm": None, "vllm-ascend": "0.33.0+custom"}
    assert paths.resolve_log_cli_capabilities(versions) == VllmLogCliCapabilities(runtime_paths=True)


@pytest.mark.parametrize("configured", [{}, {"additional_config": {"runtime_report_dir": None}}])
def test_default_paths_do_not_enable_optional_features(configured):
    paths.apply_log_cli_paths(configured, ROOT, ALL_CAPABILITIES)
    assert configured == {
        "additional_config": {
            "ascend_log_path": str(ROOT / "vllm_ascend"),
            "runtime_config_path": str(ROOT / "runtime/config/runtime_config.json"),
            "runtime_report_dir": str(ROOT / "runtime/report"),
            "runtime_dump_dir": str(ROOT / "runtime/kv_cache"),
        }
    }


@pytest.mark.parametrize("representation", ["object", "json", "dotted"])
def test_explicit_paths_and_watchdog_settings_are_preserved(representation):
    additional = {
        "ascend_log_path": "/user/ascend//logs/",
        "runtime_config_path": "/user/runtime/config.json",
        "runtime_report_dir": "/user/reports",
        "runtime_dump_dir": "/user/kv-cache",
    }
    watchdog = {"dump_dir": "/user/stacks/", "timeout": 60}
    configured = {"additional_config": additional, "watchdog_config": watchdog}
    if representation == "json":
        configured = {key: json.dumps(value) for key, value in configured.items()}
    elif representation == "dotted":
        configured = {f"{key}.{field}": value for key, obj in configured.items() for field, value in obj.items()}
    configured["watchdog_config.dump_dir"] = watchdog["dump_dir"]
    paths.apply_log_cli_paths(configured, ROOT, ALL_CAPABILITIES)
    assert configured["additional_config"] == additional
    if representation == "dotted":
        assert configured["watchdog_config.timeout"] == 60
    else:
        assert configured["watchdog_config"] == watchdog
    assert configured["watchdog_config.dump_dir"] == watchdog["dump_dir"]


@pytest.mark.parametrize("json_input", [False, True])
def test_unsupported_options_are_filtered_before_conflicts_without_changing_runtime_rules(json_input):
    rules = {"detector": {"logits_finite": {"enabled": True}}}
    additional = {"runtime_report_dir": "/one", "runtime_config": rules, "runtime_config_hot_reload": True}
    configured = {
        "additional_config": json.dumps(additional) if json_input else additional,
        "additional_config.runtime_report_dir": "/two",
        "additional_config.runtime_dump_dir": "/dump",
        "additional_config.runtime_config_path": "/config.json",
        "watchdog_config": {"dump_dir": "/one", "timeout": 60},
        "watchdog_config.dump_dir": "/two",
        "max_num_seqs": 4,
    }
    paths.apply_log_cli_paths(configured, ROOT, VllmLogCliCapabilities())
    assert configured == {
        "additional_config": {
            "ascend_log_path": str(ROOT / "vllm_ascend"),
            "runtime_config": rules,
            "runtime_config_hot_reload": True,
        },
        "max_num_seqs": 4,
    }


def test_dotted_fields_merge_with_matching_nested_values():
    rules = {"detector": {"token_repeat": {"enabled": False}}}
    configured = {
        "additional_config": {"runtime_config": rules, "runtime_report_dir": "/user/report"},
        "additional_config.runtime_config.detector.token_repeat.enabled": False,
        "additional_config.runtime_config.dump": {"auto_max_times": 0},
        "additional_config.runtime_report_dir": "/user/report",
    }
    paths.apply_log_cli_paths(configured, ROOT, ALL_CAPABILITIES)
    assert set(configured) == {"additional_config"}
    assert configured["additional_config"]["runtime_config"] == {**rules, "dump": {"auto_max_times": 0}}
    assert configured["additional_config"]["runtime_report_dir"] == "/user/report"


@pytest.mark.parametrize(
    "configured",
    [
        {"watchdog_config": {"timeout": 60}},
        {"watchdog_config": '{"dump_dir": "", "timeout": 60}'},
        {"watchdog_config": {"dump_dir": None}},
        {"watchdog_config.dump_dir": ""},
    ],
)
def test_missing_or_empty_watchdog_dump_path_preserves_native_settings(configured):
    before = deepcopy(configured)
    paths.apply_log_cli_paths(configured, ROOT, ALL_CAPABILITIES)
    configured.pop("additional_config")
    assert configured == before


@pytest.mark.parametrize(
    "configured",
    [
        {"additional_config": "{invalid JSON"},
        {"additional_config": []},
        {"additional_config.runtime_config..enabled": True},
        {"additional_config": {"ascend_log_path": "relative"}},
        {"additional_config": {"runtime_report_dir": "/logs/../report"}},
        {"additional_config": {"runtime_dump_dir": 42}},
        {"additional_config": {"runtime_config_path": ""}},
        {"additional_config": {"runtime_report_dir": "/one"}, "additional_config.runtime_report_dir": "/two"},
        {"watchdog_config": {"dump_dir": "relative"}},
        {"watchdog_config.dump_dir": "/logs/../stacks"},
        {"watchdog_config": '{"dump_dir": 42}'},
        {"watchdog_config": {"dump_dir": "/one"}, "watchdog_config.dump_dir": "/two"},
        {"watchdog_config": '{"dump_dir": null}', "watchdog_config.dump_dir": "/two"},
    ],
)
def test_invalid_paths_and_conflicts_leave_input_unchanged(configured):
    before = deepcopy(configured)
    with pytest.raises(ValueError):
        paths.apply_log_cli_paths(configured, ROOT, ALL_CAPABILITIES)
    assert configured == before
