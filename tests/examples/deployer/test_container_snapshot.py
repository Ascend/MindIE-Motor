# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from pathlib import Path

import pytest

import lib.constant as C
from lib.container_snapshot import resolve_infer_service_template, validate_container_snapshot_config
from lib.generator.infer_service import _find_infer_service_set_doc, generate_yaml_infer_service_set, get_infer_role
from lib.utils import load_yaml


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"


def _snapshot_user_config(hardware_type=C.HARDWARE_TYPE_800I_A3):
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "snapshot-test",
            C.IMAGE_NAME: "mindie:test",
            C.HARDWARE_TYPE: hardware_type,
            C.P_INSTANCES_NUM: 1,
            C.D_INSTANCES_NUM: 1,
            C.SINGER_P_INSTANCES_NUM: 1,
            C.SINGER_D_INSTANCES_NUM: 1,
            C.P_POD_NPU_NUM: 1,
            C.D_POD_NPU_NUM: 1,
        },
        C.MOTOR_ENGINE_PREFILL_CONFIG: {},
        C.MOTOR_ENGINE_DECODE_CONFIG: {},
        C.MOTOR_CONTAINER_SNAPSHOT_CONFIG: {
            C.ENABLE_SNAPSHOT: True,
            C.HOST_SNAPSHOT_IMAGE_PATH: "/shared/container-snapshots",
            C.SNAPSHOT_MNT_PATH: "/mnt/runtime-data",
            C.DEVICE_SNAPSHOT_WEIGHT_PATH: "/shared/snapshot-weights",
        },
    }


def _role_snapshot_values(infer_doc, role_name):
    role = get_infer_role(infer_doc, role_name)
    pod_spec = role[C.SPEC][C.TEMPLATE][C.SPEC]
    container = pod_spec[C.CONTAINERS][0]
    env = {item[C.NAME]: item.get(C.VALUE) for item in container[C.ENV]}
    mounts = {item[C.NAME]: item for item in container[C.VOLUME_MOUNTS]}
    volumes = {item[C.NAME]: item for item in pod_spec[C.VOLUMES]}
    return env, mounts, volumes


def test_snapshot_config_uses_common_template():
    paths = {"infer_service_input_yaml": "normal.yaml"}

    assert resolve_infer_service_template(paths, _snapshot_user_config()) == "normal.yaml"
    assert resolve_infer_service_template(paths, {C.MOTOR_DEPLOY_CONFIG: {}}) == "normal.yaml"


def test_custom_snapshot_scenario_does_not_require_mindcluster_paths():
    paths = {"infer_service_input_yaml": "normal.yaml"}
    user_config = {
        C.MOTOR_DEPLOY_CONFIG: {},
        C.MOTOR_CONTAINER_SNAPSHOT_CONFIG: {
            C.ENABLE_SNAPSHOT: True,
            C.SNAPSHOT_METADATA_PATH: "/custom/snapshot_metadata.json",
        },
    }

    assert validate_container_snapshot_config(user_config) == {}
    assert resolve_infer_service_template(paths, user_config) == "normal.yaml"


@pytest.mark.parametrize(
    ("host_snapshot_path", "mnt_path"),
    [
        ("/mnt/snapshots", "/mnt"),
        ("/mnt", "/mnt/snapshots"),
        ("/mnt/snapshots", "/mnt/snapshots"),
    ],
)
def test_snapshot_config_rejects_overlapping_host_snapshot_and_mnt_paths(host_snapshot_path, mnt_path):
    user_config = _snapshot_user_config()
    snapshot_config = user_config[C.MOTOR_CONTAINER_SNAPSHOT_CONFIG]
    snapshot_config[C.HOST_SNAPSHOT_IMAGE_PATH] = host_snapshot_path
    snapshot_config[C.SNAPSHOT_MNT_PATH] = mnt_path

    with pytest.raises(ValueError, match="must not overlap"):
        validate_container_snapshot_config(user_config)


def test_common_template_renders_snapshot_deltas_and_a3_device(tmp_path):
    output = tmp_path / "infer_service.yaml"
    generate_yaml_infer_service_set(
        str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        str(output),
        _snapshot_user_config(),
    )
    infer_doc = _find_infer_service_set_doc(load_yaml(str(output), False))

    for role_name in (C.ROLE_PREFILL, C.ROLE_DECODE, C.ROLE_UNION):
        role = get_infer_role(infer_doc, role_name)
        assert role[C.METADATA][C.LABELS]["infer.huawei.com/container-snapshot"] == "true"
        pod_template = role[C.SPEC][C.TEMPLATE]
        assert pod_template[C.METADATA][C.LABELS][C.FAULT_SCHEDULING_LABEL] == "external-force"
        container = pod_template[C.SPEC][C.CONTAINERS][0]
        assert container["readinessProbe"] == {
            "exec": {"command": ["bash", "-c", "$CONFIGMAP_PATH/probe.sh readiness"]},
            "periodSeconds": 5,
            "timeoutSeconds": 4,
            "failureThreshold": 12,
        }
        env, mounts, volumes = _role_snapshot_values(infer_doc, role_name)
        assert env["CRIU_LOG_LEVEL"] == "3"
        assert env[C.SNAPSHOT_HOST_DIR_ENV] == "/shared/container-snapshots"
        assert mounts[C.SNAPSHOT_MNT][C.MOUNT_PATH] == "/mnt/runtime-data"
        assert volumes[C.SNAPSHOT_MNT][C.HOST_PATH][C.PATH] == "/mnt/runtime-data"
        assert mounts[C.SNAPSHOT_WEIGHT][C.MOUNT_PATH] == "/snapshot/weight"
        assert volumes[C.SNAPSHOT_WEIGHT][C.HOST_PATH] == {
            C.PATH: "/shared/snapshot-weights",
            C.STORAGE_TYPE: "DirectoryOrCreate",
        }
        assert mounts[C.LQDCMI_PCIDEV][C.MOUNT_PATH] == "/dev/lqdcmi_pcidev"
        assert volumes[C.LQDCMI_PCIDEV][C.HOST_PATH] == {
            C.PATH: "/dev/lqdcmi_pcidev",
            C.STORAGE_TYPE: "CharDevice",
        }
        assert mounts["ascend-driver"] == {
            C.NAME: "ascend-driver",
            C.MOUNT_PATH: "/usr/local/Ascend/driver",
            "mountPropagation": "HostToContainer",
        }
        assert mounts["dcmi"][C.MOUNT_PATH] == "/usr/local/dcmi"
        assert mounts["npu-smi"][C.MOUNT_PATH] == "/usr/local/bin/npu-smi"
        assert volumes["dcmi"][C.HOST_PATH] == {C.PATH: "/usr/local/dcmi"}
        assert volumes["npu-smi"][C.HOST_PATH] == {C.PATH: "/usr/local/bin/npu-smi"}
        for removed_name in ("data", "dshm", "coredump", "plog-path", "cache-path"):
            assert removed_name not in mounts
            assert removed_name not in volumes


def test_common_template_does_not_mount_a3_device_on_a2(tmp_path):
    output = tmp_path / "infer_service.yaml"
    generate_yaml_infer_service_set(
        str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        str(output),
        _snapshot_user_config(C.HARDWARE_TYPE_800I_A2),
    )
    infer_doc = _find_infer_service_set_doc(load_yaml(str(output), False))
    _, mounts, volumes = _role_snapshot_values(infer_doc, C.ROLE_PREFILL)

    assert C.LQDCMI_PCIDEV not in mounts
    assert C.LQDCMI_PCIDEV not in volumes


def test_common_template_keeps_standard_storage_when_snapshot_is_disabled(tmp_path):
    output = tmp_path / "infer_service.yaml"
    user_config = _snapshot_user_config(C.HARDWARE_TYPE_800I_A2)
    user_config.pop(C.MOTOR_CONTAINER_SNAPSHOT_CONFIG)
    generate_yaml_infer_service_set(
        str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        str(output),
        user_config,
    )
    infer_doc = _find_infer_service_set_doc(load_yaml(str(output), False))
    role = get_infer_role(infer_doc, C.ROLE_PREFILL)
    assert "infer.huawei.com/container-snapshot" not in role[C.METADATA][C.LABELS]
    container = role[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    assert "readinessProbe" not in container
    _, mounts, volumes = _role_snapshot_values(infer_doc, C.ROLE_PREFILL)
    for standard_name in ("data", "dshm", "coredump", "plog-path", "cache-path"):
        assert standard_name in mounts
        assert standard_name in volumes
