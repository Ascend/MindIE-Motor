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

import deploy as deploy_module


EXPECTED_PRECHECK_MESSAGE = (
    "Deployment pre-check requires attention.\n"
    "The following items are not configured:\n"
    " - Inference TLS\n"
    " - Management API Key authentication\n\n"
    "Configuration guides (v3.2.0):\n"
    " - Inference TLS: https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/docs/zh/user_guide/deployment/"
    "inference_tls_security.md\n"
    " - Management API Key: https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/docs/zh/user_guide/deployment/"
    "management_api_security.md\n\n"
    "Configure the listed items and rerun the command.\n"
    "To continue with the current settings, rerun the command with --skip-check.\n"
)


def test_main_stops_before_deployment_without_skip_check(tmp_path, monkeypatch, capsys):
    user_config_path = tmp_path / "user_config.json"
    user_config_path.write_text("{}", encoding="utf-8")
    (tmp_path / "env.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deploy_module.sys, "argv", ["deploy.py", "--config_dir", str(tmp_path)])
    monkeypatch.setattr(
        deploy_module,
        "validate_reserved_labels",
        lambda *_args, **_kwargs: pytest.fail("deployment flow must not continue without --skip-check"),
    )

    with pytest.raises(SystemExit) as exc_info:
        deploy_module.main()

    assert exc_info.value.code == 1
    assert capsys.readouterr().err == EXPECTED_PRECHECK_MESSAGE


def test_main_does_not_require_skip_check_when_security_is_configured(tmp_path, monkeypatch):
    user_config_path = tmp_path / "user_config.json"
    user_config_path.write_text(
        json.dumps(
            {
                "motor_deploy_config": {"tls_config": {"infer_tls_config": {"enable_tls": True}}},
                "motor_coordinator_config": {
                    "mgmt_api_key_config": {"enable_api_key": True, "api_key_file": "/run/secrets/mgmt-key"}
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        deploy_module.sys,
        "argv",
        ["deploy.py", "--config_dir", str(tmp_path), "--dry-run"],
    )
    monkeypatch.setattr(deploy_module, "validate_reserved_labels", lambda *_args: None)
    monkeypatch.setattr(deploy_module, "validate_container_snapshot_config", lambda *_args: None)
    monkeypatch.setattr(deploy_module, "validate_instance_nums", lambda *_args: None)
    deployed = []
    monkeypatch.setattr(deploy_module, "deploy_services", lambda *_args, **_kwargs: deployed.append(True))

    deploy_module.main()

    assert deployed == [True]


@pytest.mark.parametrize(
    "management_api_key_config",
    [
        {},
        {"enable_api_key": True},
        {"enable_api_key": True, "api_key_file": ""},
    ],
)
def test_security_is_not_configured_without_management_auth_and_key(management_api_key_config):
    user_config = {
        "motor_deploy_config": {"tls_config": {"infer_tls_config": {"enable_tls": True}}},
        "motor_coordinator_config": {"mgmt_api_key_config": management_api_key_config},
    }

    assert deploy_module.is_security_configured(user_config) is False


def test_parse_arguments_accepts_skip_check(monkeypatch):
    monkeypatch.setattr(deploy_module.sys, "argv", ["deploy.py", "--skip-check"])

    args = deploy_module.parse_arguments()

    assert args.skip_check


def test_main_updates_scaling_policy_without_deploy_config(monkeypatch):
    updated = []
    monkeypatch.setattr(
        deploy_module.sys,
        "argv",
        ["deploy.py", "--update_scaling_policy", "policy.json"],
    )
    monkeypatch.setattr(deploy_module, "update_scaling_policy", lambda path, **options: updated.append(path))

    deploy_module.main()

    assert updated == ["policy.json"]


@pytest.mark.parametrize("mode", ["rbg", "infer_service_set", "multi_deployment"])
def test_hybrid_main_resolves_infer_template_only_for_infer_mode(tmp_path, monkeypatch, mode):
    config = {
        "motor_deploy_config": {"deploy_mode": mode, "hybrid_instances_num": 1},
        "motor_engine_union_config": {},
    }
    (tmp_path / "user_config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "env.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        deploy_module.sys, "argv", ["deploy.py", "--config_dir", str(tmp_path), "--skip-check", "--dry-run"]
    )
    monkeypatch.setattr(deploy_module.C, "OUTPUT_ROOT_PATH", str(tmp_path / "output"))
    for name in (
        "validate_reserved_labels",
        "validate_kv_cache_store_config",
        "validate_container_snapshot_config",
        "validate_instance_nums",
    ):
        monkeypatch.setattr(deploy_module, name, lambda *_: None)
    checked = []
    monkeypatch.setattr(deploy_module, "validate_pd_hybrid_config", lambda *_: checked.append("config"))

    def resolve(*_):
        assert mode == "infer_service_set", "Non-Infer deployment must not resolve the Infer template"
        return "infer.yaml"

    monkeypatch.setattr(deploy_module, "resolve_infer_service_template", resolve)
    monkeypatch.setattr(
        deploy_module, "validate_pd_hybrid_infer_service_template", lambda _, path: checked.append(path)
    )
    monkeypatch.setattr(deploy_module, "deploy_services", lambda *a, **kw: checked.append("deploy"))
    deploy_module.main()
    assert checked == (["config", "infer.yaml", "deploy"] if mode == "infer_service_set" else ["config", "deploy"])


@pytest.mark.parametrize("mode", ["infer_service_set", "multi_deployment", "single_container", "rbg"])
@pytest.mark.parametrize(
    "flags",
    [
        ["--update_config", "--update_instance_num"],
        ["--dry-run", "--update_config"],
        ["--dry-run", "--update_instance_num"],
    ],
)
def test_update_argument_checks_are_rbg_only(monkeypatch, tmp_path, mode, flags):
    config = {"motor_deploy_config": {"deploy_mode": mode, "job_id": "demo"}}
    (tmp_path / "user_config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "env.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deploy_module.sys, "argv", ["deploy.py", "--config_dir", str(tmp_path), "--skip-check", *flags])
    for name in (
        "validate_service_identity",
        "validate_reserved_labels",
        "validate_kv_cache_store_config",
        "validate_container_snapshot_config",
        "validate_instance_nums",
        "sync_decode_first_token_timeout_from_env",
    ):
        monkeypatch.setattr(deploy_module, name, lambda *a, **kw: None)
    monkeypatch.setattr(deploy_module, "output_root", lambda: str(tmp_path / "output"))
    calls = []
    monkeypatch.setattr(deploy_module, "handle_update_config", lambda *a: calls.append("config"))
    monkeypatch.setattr(deploy_module, "handle_update_instance_num", lambda *a: calls.append("scale"))
    if mode == "rbg":
        with pytest.raises(ValueError, match="RBG"):
            deploy_module.main()
        assert not calls
    else:
        deploy_module.main()
        assert calls == (["config"] if "--update_config" in flags else ["scale"])


@pytest.mark.parametrize("other", ["--update_config", "--update_instance_num", "--dry-run"])
def test_policy_flags_are_forwarded_after_parsing(monkeypatch, other):
    calls = []
    monkeypatch.setattr(deploy_module.sys, "argv", ["deploy.py", "--update_scaling_policy", "policy.json", other])
    monkeypatch.setattr(deploy_module, "update_scaling_policy", lambda path, **options: calls.append((path, options)))
    deploy_module.main()
    assert calls == [("policy.json", {"dry_run": other == "--dry-run", "other_update": other != "--dry-run"})]
