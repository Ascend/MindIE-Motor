# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# tests/examples/deployer/test_generator_utils.py
import sys
from pathlib import Path
import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.generator.storage import get_storage_entries, apply_dshm_size  # noqa: E402
from lib.generator.engine import set_container_npu  # noqa: E402
from lib.generator.k8s_utils import apply_additional_labels_annotations  # noqa: E402


def test_get_storage_entries_validation():
    # 空配置
    assert get_storage_entries({}) == []

    # 合法的 PVC 配置
    user_config = {
        C.MOTOR_DEPLOY_CONFIG: {
            C.STORAGE: [{C.STORAGE_TYPE: C.STORAGE_TYPE_PVC, C.STORAGE_CLASS: "csi-class", C.STORAGE_SIZE: "100Gi"}]
        }
    }
    entries = get_storage_entries(user_config)
    assert len(entries) == 1

    # 非法的 size（无单位）
    user_config[C.MOTOR_DEPLOY_CONFIG][C.STORAGE][0][C.STORAGE_SIZE] = "100"
    with pytest.raises(ValueError, match="has no unit"):
        get_storage_entries(user_config)


def test_apply_dshm_size():
    pod_spec = {C.VOLUMES: [{C.NAME: C.DSHM_VOLUME, C.EMPTY_DIR: {}}]}

    # 正常设置
    user_config = {C.MOTOR_DEPLOY_CONFIG: {C.DSHM_SIZE: "128Gi"}}
    apply_dshm_size(pod_spec, user_config)
    assert pod_spec[C.VOLUMES][0][C.EMPTY_DIR][C.SIZE_LIMIT] == "128Gi"

    # 非法布尔值报错
    user_config = {C.MOTOR_DEPLOY_CONFIG: {C.DSHM_SIZE: True}}
    with pytest.raises(ValueError, match="is not a size"):
        apply_dshm_size(pod_spec, user_config)


def test_set_container_npu_hardware_difference():
    # A2 硬件分配 Ascend 910 (使用 sorted 避免 set 无序导致的不稳定)
    container = {C.RESOURCES: {C.REQUESTS: {}, C.LIMITS: {}}}
    deploy_config = {C.HARDWARE_TYPE: sorted(C.HARDWARE_TYPE_A2)[0]}
    set_container_npu(container, 8, deploy_config)
    assert container[C.RESOURCES][C.REQUESTS][C.ASCEND_910_NPU_NUM] == 8

    # A5 硬件分配 Ascend 950 (使用 sorted)
    container = {C.RESOURCES: {C.REQUESTS: {}, C.LIMITS: {}}}
    deploy_config = {C.HARDWARE_TYPE: sorted(C.HARDWARE_TYPE_A5)[0]}
    set_container_npu(container, 16, deploy_config)
    assert container[C.RESOURCES][C.REQUESTS][C.ASCEND_950_NPU_NUM] == 16


def test_apply_additional_labels_annotations():
    # 测试标签和注解是否正确合并到 Deployment 和 Pod Template 中
    workload = {C.METADATA: {C.LABELS: {"exist": "val"}}, C.SPEC: {C.TEMPLATE: {C.METADATA: {}}}}
    role_config = {C.ADDITIONAL_LABELS: {"new_label": "test"}, C.ADDITIONAL_ANNOTATIONS: {"new_annotation": "test"}}

    apply_additional_labels_annotations(workload, role_config)

    assert workload[C.METADATA][C.LABELS]["new_label"] == "test"
    assert workload[C.METADATA][C.ANNOTATIONS]["new_annotation"] == "test"
    assert workload[C.SPEC][C.TEMPLATE][C.METADATA][C.LABELS]["new_label"] == "test"
