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
import tempfile
from pathlib import Path

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.a5_host_nic import (  # noqa: E402
    apply_a5_host_nic_privileged,
    env_requests_a5_host_nic_overlay,
)
from lib.generator import k8s_utils  # noqa: E402
from lib.generator.engine import apply_a5_engine_pod_config  # noqa: E402


def test_env_requests_overlay_detects_protocol_desc_tokens():
    assert env_requests_a5_host_nic_overlay(
        {
            "motor_engine_prefill_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.protocol_desc":["uboe:device"]}'
            }
        }
    )
    assert env_requests_a5_host_nic_overlay(
        {
            "motor_engine_decode_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.protocol_desc":"roce:device"}'
            }
        }
    )
    assert env_requests_a5_host_nic_overlay(
        {
            "motor_common_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": {"comm_resource_config.protocol_desc": ["ub_rtp:device"]}
            }
        }
    )
    # Socket IFNAME alone no longer enables overlay.
    assert not env_requests_a5_host_nic_overlay({"motor_engine_prefill_env": {"GLOO_SOCKET_IFNAME": "eth0"}})
    assert not env_requests_a5_host_nic_overlay(
        {"motor_engine_prefill_env": {"ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.listen_port": "26666"}'}}
    )
    assert not env_requests_a5_host_nic_overlay({})


def test_apply_a5_engine_pod_config_default_path_no_host_network():
    k8s_utils.g_a5_host_nic_overlay = False
    pod_spec = {C.VOLUMES: []}
    container = {C.VOLUME_MOUNTS: [], C.ENV: []}
    deploy_config = {C.HARDWARE_TYPE: C.HARDWARE_TYPE_ASCEND950}

    apply_a5_engine_pod_config(pod_spec, container, deploy_config)

    assert C.HOST_NETWORK not in pod_spec
    assert C.SECURITY_CONTEXT not in container
    assert not any(item.get(C.NAME) == "HCCL_IF_IP" for item in container[C.ENV])
    mounted = {m[C.NAME] for m in container[C.VOLUME_MOUNTS]}
    assert "hixlep" in mounted
    assert "hccl-rootinfo" in mounted
    assert "npu-smi-bin" not in mounted


def test_apply_a5_engine_pod_config_host_nic_overlay_when_enabled():
    k8s_utils.g_a5_host_nic_overlay = True
    pod_spec = {C.VOLUMES: []}
    container = {C.VOLUME_MOUNTS: [], C.ENV: []}
    deploy_config = {C.HARDWARE_TYPE: C.HARDWARE_TYPE_ASCEND950}

    apply_a5_engine_pod_config(pod_spec, container, deploy_config)

    assert pod_spec[C.HOST_NETWORK] is True
    assert pod_spec[C.DNS_POLICY] == C.DNS_POLICY_CLUSTER_FIRST_WITH_HOST_NET
    assert container[C.SECURITY_CONTEXT][C.PRIVILEGED] is True
    hccl = next(item for item in container[C.ENV] if item[C.NAME] == "HCCL_IF_IP")
    assert hccl["valueFrom"]["fieldRef"]["fieldPath"] == "status.hostIP"
    mounted = {m[C.NAME] for m in container[C.VOLUME_MOUNTS]}
    assert "npu-smi-bin" in mounted
    assert "hccl-rootinfo" in mounted
    assert "dcmi" in mounted
    by_name = {v[C.NAME]: v for v in pod_spec[C.VOLUMES]}
    assert by_name["hccl-rootinfo"][C.HOST_PATH][C.PATH] == "/etc/hccl_rootinfo.json"
    assert not any(name.startswith("atlas-topo") for name in mounted)


def test_apply_a5_engine_pod_config_skips_non_a5():
    k8s_utils.g_a5_host_nic_overlay = True
    pod_spec = {}
    container = {C.ENV: []}
    deploy_config = {C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A2}

    apply_a5_engine_pod_config(pod_spec, container, deploy_config)

    assert C.HOST_NETWORK not in pod_spec
    assert not container[C.ENV]


def _write_env(tmpdir: Path, protocol_desc) -> str:
    payload = {
        "motor_engine_prefill_env": {
            "ASCEND_GLOBAL_RESOURCE_CONFIG": {
                "comm_resource_config.protocol_desc": protocol_desc,
            }
        }
    }
    path = tmpdir / "env.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_apply_a5_host_nic_privileged_uboe_and_roce():
    base = C.ENTER_DOCKER_RUN_A5
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        uboe = apply_a5_host_nic_privileged(
            base, C.HARDWARE_TYPE_ASCEND950, _write_env(tmpdir, ["uboe:device"]), attach_npu=True
        )
        assert uboe.startswith("docker run --privileged ")
        assert "--privileged" in uboe
        roce = apply_a5_host_nic_privileged(
            base, C.HARDWARE_TYPE_ASCEND950, _write_env(tmpdir, "roce:device"), attach_npu=True
        )
        assert roce.startswith("docker run --privileged ")


def test_apply_a5_host_nic_privileged_skipped_without_agrc_or_non_a5():
    base = C.ENTER_DOCKER_RUN_A5
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        path = tmpdir / "env.json"
        path.write_text(
            json.dumps(
                {
                    "motor_engine_prefill_env": {
                        "ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.listen_port": "26666"}'
                    }
                }
            ),
            encoding="utf-8",
        )
        assert apply_a5_host_nic_privileged(base, C.HARDWARE_TYPE_ASCEND950, str(path), attach_npu=True) == base
        assert (
            apply_a5_host_nic_privileged(
                base, C.HARDWARE_TYPE_800I_A2, _write_env(tmpdir, ["uboe:device"]), attach_npu=True
            )
            == base
        )
        assert (
            apply_a5_host_nic_privileged(
                base, C.HARDWARE_TYPE_ASCEND950, _write_env(tmpdir, ["uboe:device"]), attach_npu=False
            )
            == base
        )


def test_apply_a5_host_nic_privileged_idempotent():
    already = 'docker run --privileged -it --name "$NAME" bash'
    with tempfile.TemporaryDirectory() as tmp:
        out = apply_a5_host_nic_privileged(
            already,
            C.HARDWARE_TYPE_ASCEND950,
            _write_env(Path(tmp), ["ub_rtp:device"]),
            attach_npu=True,
        )
    assert out == already
    assert out.count("--privileged") == 1
