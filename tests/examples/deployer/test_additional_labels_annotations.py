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
import lib.constant as C
from lib.config_validator import validate_reserved_labels
from lib.generator.k8s_utils import apply_additional_labels_annotations


def test_apply_additional_labels_annotations_to_deployment_and_pod_template():
    deployment = {
        C.METADATA: {
            C.ANNOTATIONS: {"existing-annotation": "deployment-value", "shared-annotation": "old-value"},
            C.LABELS: {"existing-label": "deployment-value", "shared-label": "old-value"},
        },
        C.SPEC: {
            C.TEMPLATE: {
                C.METADATA: {
                    C.ANNOTATIONS: {"existing-annotation": "pod-value", "shared-annotation": "old-value"},
                    C.LABELS: {"existing-label": "pod-value", "shared-label": "old-value"},
                }
            }
        },
    }
    role_config = {
        "additional_annotations": {
            "new-annotation": "new-value",
            "shared-annotation": "overridden-value",
        },
        "additional_labels": {
            "new-label": "new-value",
            "shared-label": "overridden-value",
        },
    }

    apply_additional_labels_annotations(deployment, role_config)

    for metadata, existing_value in [
        (deployment[C.METADATA], "deployment-value"),
        (deployment[C.SPEC][C.TEMPLATE][C.METADATA], "pod-value"),
    ]:
        assert metadata[C.ANNOTATIONS] == {
            "existing-annotation": existing_value,
            "new-annotation": "new-value",
            "shared-annotation": "overridden-value",
        }
        assert metadata[C.LABELS] == {
            "existing-label": existing_value,
            "new-label": "new-value",
            "shared-label": "overridden-value",
        }


def test_validate_reserved_labels_accepts_non_reserved_labels():
    user_config = {
        C.MOTOR_CONTROLLER_CONFIG: {C.ADDITIONAL_LABELS: {"team": "inference"}},
        C.MOTOR_ENGINE_PREFILL_CONFIG: {C.ADDITIONAL_LABELS: {}},
    }

    validate_reserved_labels(user_config)


def test_validate_reserved_labels_reports_all_conflicting_sections():
    user_config = {
        C.MOTOR_CONTROLLER_CONFIG: {C.ADDITIONAL_LABELS: {"app": "custom-controller"}},
        C.MOTOR_ENGINE_PREFILL_CONFIG: {
            C.ADDITIONAL_LABELS: {
                "app": "custom-prefill",
                "team": "inference",
            }
        },
    }

    with pytest.raises(ValueError) as exc_info:
        validate_reserved_labels(user_config)

    error_message = str(exc_info.value)
    assert C.MOTOR_CONTROLLER_CONFIG in error_message
    assert C.MOTOR_ENGINE_PREFILL_CONFIG in error_message
    assert "app" in error_message


def test_main_rejects_reserved_labels_before_deployment(tmp_path, monkeypatch):
    user_config_path = tmp_path / "user_config.json"
    env_config_path = tmp_path / "env.json"
    user_config_path.write_text(
        json.dumps(
            {
                C.MOTOR_DEPLOY_CONFIG: {},
                C.MOTOR_CONTROLLER_CONFIG: {C.ADDITIONAL_LABELS: {"app": "custom-controller"}},
            }
        ),
        encoding="utf-8",
    )
    env_config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(tmp_path / "output"))
    monkeypatch.setattr(
        deploy_module.sys,
        "argv",
        ["deploy.py", "--config", str(user_config_path), "--env", str(env_config_path)],
    )
    monkeypatch.setattr(
        deploy_module,
        "deploy_services",
        lambda *_args, **_kwargs: pytest.fail("deployment must not start when reserved labels are configured"),
    )

    with pytest.raises(ValueError, match="reserved.*app"):
        deploy_module.main()
