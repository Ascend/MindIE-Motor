# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest
import docker_deploy

from lib.log_mount import apply_document_log_mounts, common_log_root


def test_common_root_only_and_invalid_path():
    env = {
        "motor_common_env": {"MOTOR_LOG_ROOT_PATH": "/data/log"},
        "motor_engine_prefill_env": {"MOTOR_LOG_ROOT_PATH": "/ignored"},
    }
    assert common_log_root(env) == "/data/log"
    assert common_log_root({}) == "/root/ascend/log"
    with pytest.raises(ValueError):
        common_log_root({"motor_common_env": {"MOTOR_LOG_ROOT_PATH": "../log"}})


@pytest.mark.parametrize("host_root", [None, "/host/log", "/root/ascend/log"])
def test_workload_mount_respects_existing_host_configuration(host_root):
    pod = {
        "containers": [
            {
                "env": [{"name": "ROLE", "value": "prefill"}],
                "volumeMounts": [{"name": "plog-path", "mountPath": "/root/ascend/log"}],
            }
        ],
        "volumes": [{"name": "plog-path", "hostPath": {"path": host_root or "/root/ascend/log"}}],
    }
    config = {"motor_nodemanger_config": {"logging_config": {"host_log_dir": host_root}}} if host_root else {}
    docs = [{"kind": "Deployment", "spec": {"template": {"spec": pod}}}]
    apply_document_log_mounts(docs, "/data/log", config)
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/data/log"
    assert pod["volumes"][0]["hostPath"]["path"] == (host_root or "/data/log")


def test_existing_mount_destination_is_not_overwritten():
    pod = {
        "containers": [
            {
                "volumeMounts": [
                    {"name": "plog-path", "mountPath": "/root/ascend/log"},
                    {"name": "other", "mountPath": "/data/log"},
                ]
            }
        ],
        "volumes": [{"name": "plog-path", "hostPath": {"path": "/root/ascend/log"}}],
    }
    with pytest.raises(ValueError, match="already used"):
        apply_document_log_mounts(pod, "/data/log")


@pytest.mark.parametrize(
    "binds",
    [
        ["/host/data:/data"],
        ["/host/data:/data:ro", "/host/log:/data/log:rw"],
        ["/host/data:/data", "/ignored:/data:ro"],
    ],
)
def test_docker_log_bind_preserves_existing_effective_mount(monkeypatch, binds):
    template = 'docker run \\\n' + ''.join(f'  -v {spec} \\\n' for spec in binds) + '  "$IMAGE" bash\n'
    monkeypatch.setattr(docker_deploy.os, "makedirs", lambda *args, **kwargs: pytest.fail("unexpected mkdir"))
    assert docker_deploy.apply_log_root_bind(template, "/data/log", {}) == template


@pytest.mark.parametrize(
    "binds, env",
    [
        (['"/host/data:/data:ro"'], {}),
        (["/data:/data", '"$WEIGHT:$WEIGHT:ro"'], {"WEIGHT": "/data/models"}),
    ],
)
def test_docker_log_bind_rejects_effective_readonly_mount(monkeypatch, binds, env):
    template = 'docker run \\\n' + ''.join(f'  -v {spec} \\\n' for spec in binds) + '  "$IMAGE" bash\n'
    monkeypatch.setattr(docker_deploy.os, "makedirs", lambda *args, **kwargs: pytest.fail("unexpected mkdir"))
    with pytest.raises(ValueError, match="read-only bind"):
        docker_deploy.apply_log_root_bind(template, "/data/models/log", env)


def test_docker_log_bind_adds_custom_root(monkeypatch):
    template = 'docker run \\\n  -v /root/ascend/log:/root/ascend/log \\\n  "$IMAGE" bash\n'
    created = []
    monkeypatch.setattr(docker_deploy.os, "makedirs", lambda path, **kwargs: created.append(path))
    result = docker_deploy.apply_log_root_bind(template, "/logs/custom", {})
    assert "-v /logs/custom:/logs/custom" in result
    assert created == ["/logs/custom"]
