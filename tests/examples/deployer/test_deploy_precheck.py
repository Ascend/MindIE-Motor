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
