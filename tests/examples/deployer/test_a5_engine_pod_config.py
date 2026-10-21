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
    agrc_requests_ubg_host_network,
    apply_a5_host_nic_privileged,
    env_requests_a5_host_nic_overlay,
    env_requests_a5_ubg_host_network,
)
from lib.generator import k8s_utils  # noqa: E402
from lib.generator.engine import (  # noqa: E402
    apply_a5_engine_pod_config,
    update_a5_host_nic_overlay_from_env,
)


def test_env_requests_overlay_detects_protocol_desc_tokens():
    assert env_requests_a5_host_nic_overlay(
        {
            "motor_engine_prefill_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.protocol_desc":["uboe:device"]}'
            }
        }
    )
    assert not env_requests_a5_host_nic_overlay(
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
    assert env_requests_a5_host_nic_overlay(
        {
            "motor_engine_prefill_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.protocol_desc":["ub_ctp:device"]}'
            }
        }
    )
    # Socket IFNAME alone no longer enables overlay.
    assert not env_requests_a5_host_nic_overlay({"motor_engine_prefill_env": {"GLOO_SOCKET_IFNAME": "eth0"}})
    assert not env_requests_a5_host_nic_overlay(
        {"motor_engine_prefill_env": {"ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.listen_port": "26666"}'}}
    )
    assert not env_requests_a5_host_nic_overlay({})
    assert env_requests_a5_ubg_host_network(
        {
            "motor_common_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": {"comm_resource_config.protocol_desc": ["ub_rtp:device"]}
            }
        }
    )
    assert not env_requests_a5_ubg_host_network(
        {
            "motor_engine_prefill_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.protocol_desc":["uboe:device"]}'
            }
        }
    )
    assert not env_requests_a5_ubg_host_network(
        {
            "motor_engine_decode_env": {
                "ASCEND_GLOBAL_RESOURCE_CONFIG": '{"comm_resource_config.protocol_desc":"roce:device"}'
            }
        }
    )


def test_apply_a5_engine_pod_config_default_path_no_host_network():
    k8s_utils.g_a5_host_nic_overlay = False
    k8s_utils.g_a5_ubg_host_network = False
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
    k8s_utils.g_a5_ubg_host_network = False
    pod_spec = {C.VOLUMES: []}
    container = {C.VOLUME_MOUNTS: [], C.ENV: []}
    deploy_config = {C.HARDWARE_TYPE: C.HARDWARE_TYPE_ASCEND950}

    apply_a5_engine_pod_config(pod_spec, container, deploy_config)

    assert C.HOST_NETWORK not in pod_spec
    assert pod_spec.get(C.DNS_POLICY) != C.DNS_POLICY_CLUSTER_FIRST_WITH_HOST_NET
    assert container[C.SECURITY_CONTEXT][C.PRIVILEGED] is True
    assert not any(item.get(C.NAME) == "HCCL_IF_IP" for item in container[C.ENV])
    mounted = {m[C.NAME] for m in container[C.VOLUME_MOUNTS]}
    assert "npu-smi-bin" not in mounted
    assert "hccl-rootinfo" in mounted
    assert "dcmi" in mounted
    assert "hixlep" not in mounted
    assert "dev-ummu" in mounted
    assert "dev-uburma" in mounted
    by_name = {v[C.NAME]: v for v in pod_spec[C.VOLUMES]}
    assert by_name["hccl-rootinfo"][C.HOST_PATH][C.PATH] == "/etc/hccl_rootinfo.json"
    assert by_name["dev-ummu"][C.HOST_PATH]["type"] == "Directory"
    assert by_name["dev-uburma"][C.HOST_PATH]["type"] == "Directory"
    assert not any(name.startswith("atlas-topo") for name in mounted)


def test_apply_a5_engine_pod_config_ubg_enables_host_network():
    k8s_utils.g_a5_host_nic_overlay = True
    k8s_utils.g_a5_ubg_host_network = True
    pod_spec = {C.VOLUMES: []}
    container = {C.VOLUME_MOUNTS: [], C.ENV: []}
    deploy_config = {C.HARDWARE_TYPE: C.HARDWARE_TYPE_ASCEND950_POD16}

    apply_a5_engine_pod_config(pod_spec, container, deploy_config)

    assert pod_spec[C.HOST_NETWORK] is True
    assert pod_spec[C.DNS_POLICY] == C.DNS_POLICY_CLUSTER_FIRST_WITH_HOST_NET
    assert container[C.SECURITY_CONTEXT][C.PRIVILEGED] is True
    hccl = next(item for item in container[C.ENV] if item.get(C.NAME) == "HCCL_IF_IP")
    assert hccl["valueFrom"]["fieldRef"]["fieldPath"] == "status.hostIP"
    mounted = {m[C.NAME] for m in container[C.VOLUME_MOUNTS]}
    assert "dev-ummu" in mounted
    assert "dev-uburma" in mounted
    assert "hixlep" not in mounted


def test_ubg_host_network_ignores_token_outside_protocol_desc():
    mentioned = {
        "comm_resource_config.protocol_desc": ["uboe:device"],
        "note": "do not set ub_rtp:device",
    }
    assert agrc_requests_ubg_host_network(mentioned) is False
    assert agrc_requests_ubg_host_network(json.dumps(mentioned, ensure_ascii=False)) is False
    assert agrc_requests_ubg_host_network({"comm_resource_config.protocol_desc": ["ub_rtp:device"]})
    assert agrc_requests_ubg_host_network('{"comm_resource_config.protocol_desc":["ub_rtp:device"]}')


def test_update_overlay_flags_ubg_vs_uboe(tmp_path):
    ubg = tmp_path / "ubg.json"
    ubg.write_text(
        json.dumps(
            {
                "motor_engine_prefill_env": {
                    "ASCEND_GLOBAL_RESOURCE_CONFIG": {"comm_resource_config.protocol_desc": ["ub_rtp:device"]}
                }
            }
        ),
        encoding="utf-8",
    )
    update_a5_host_nic_overlay_from_env(str(ubg))
    assert k8s_utils.g_a5_host_nic_overlay is True
    assert k8s_utils.g_a5_ubg_host_network is True

    uboe = tmp_path / "uboe.json"
    uboe.write_text(
        json.dumps(
            {
                "motor_engine_prefill_env": {
                    "ASCEND_GLOBAL_RESOURCE_CONFIG": {"comm_resource_config.protocol_desc": ["uboe:device"]}
                }
            }
        ),
        encoding="utf-8",
    )
    update_a5_host_nic_overlay_from_env(str(uboe))
    assert k8s_utils.g_a5_host_nic_overlay is True
    assert k8s_utils.g_a5_ubg_host_network is False

    roce = tmp_path / "roce.json"
    roce.write_text(
        json.dumps(
            {
                "motor_engine_prefill_env": {
                    "ASCEND_GLOBAL_RESOURCE_CONFIG": {"comm_resource_config.protocol_desc": ["roce:device"]}
                }
            }
        ),
        encoding="utf-8",
    )
    update_a5_host_nic_overlay_from_env(str(roce))
    assert k8s_utils.g_a5_host_nic_overlay is False
    assert k8s_utils.g_a5_ubg_host_network is False


def test_apply_a5_engine_pod_config_skips_non_a5():
    k8s_utils.g_a5_host_nic_overlay = True
    k8s_utils.g_a5_ubg_host_network = True
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


def test_apply_a5_host_nic_privileged_uboe_not_roce():
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
        assert roce == base


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
