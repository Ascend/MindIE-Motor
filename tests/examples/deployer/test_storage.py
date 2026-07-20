# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import logging
import sys
from pathlib import Path

import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

from lib.generator.storage import (  # noqa: E402
    is_storage_enabled,
    get_storage_entries,
    build_storage_pvc_docs,
    apply_storage_volumes,
    apply_dshm_size,
)

PVC_TEMPLATE = str(DEPLOYER_ROOT / "yaml_template" / "storage_pvc_template.yaml")


def _uc(storage=None, dshm=None):
    deploy_config = {"job_id": "mm"}
    if storage is not None:
        deploy_config["storage"] = storage
    if dshm is not None:
        deploy_config["dshm_size"] = dshm
    return {"motor_deploy_config": deploy_config}


def test_is_storage_enabled_variants():
    assert is_storage_enabled(_uc()) is False
    assert is_storage_enabled(_uc(storage=[])) is False
    assert is_storage_enabled(_uc(storage=[{"enable": False, "type": "pvc", "storage_class_name": "sc"}])) is False
    assert is_storage_enabled(_uc(storage=[{"type": "pvc", "storage_class_name": "sc"}])) is True
    # a single dict is tolerated as a one-element list
    assert is_storage_enabled(_uc(storage={"type": "pvc", "storage_class_name": "sc"})) is True


def test_enable_must_be_a_json_boolean():
    """Truthiness would silently enable "false"/"no"; only real booleans are accepted."""
    for bad in ("false", "no", "true", 0, 1):
        with pytest.raises(ValueError, match="boolean"):
            is_storage_enabled(_uc(storage=[{"enable": bad, "type": "pvc", "storage_class_name": "sc"}]))


def test_storage_must_be_a_list():
    """A non-list/non-dict storage value would silently mount nothing — reject loudly."""
    for bad in ("pvc", 123, True):
        with pytest.raises(ValueError, match="list"):
            is_storage_enabled(_uc(storage=bad))
    # an explicit `"storage": null` means "not configured", same as omitting the key
    assert is_storage_enabled({"motor_deploy_config": {"job_id": "mm", "storage": None}}) is False


def test_storage_entry_must_be_a_dict():
    """A non-dict element would silently leave that volume unmounted — reject loudly."""
    valid = {"type": "pvc", "storage_class_name": "sc"}
    for bad in ("pvc", 123, None, ["nested"]):
        with pytest.raises(ValueError, match="object"):
            is_storage_enabled(_uc(storage=[valid, bad]))


def test_read_only_must_be_a_json_boolean():
    """Truthiness would mount "false"/"no" read-only; only real booleans are accepted."""
    for bad in ("false", "no", "true", 0, 1, None):
        with pytest.raises(ValueError, match="boolean"):
            is_storage_enabled(_uc(storage=[{"type": "pvc", "storage_class_name": "sc", "read_only": bad}]))


def test_string_keys_must_be_non_empty_strings():
    """Non-string values (true/123/null/"") would be emitted verbatim into the manifest and
    only rejected at kubectl apply — reject them at validation time instead."""
    cases = [
        {"type": "pvc", "claim_name": True},
        {"type": "pvc", "claim_name": ""},
        {"type": "pvc", "storage_class_name": 123},
        {"type": "pvc", "storage_class_name": None},
        {"type": "pvc", "storage_class_name": "sc", "access_mode": True},
        {"type": "pvc", "storage_class_name": "sc", "access_mode": None},
        {"type": "nfs", "server": True, "path": "/kv"},
        {"type": "nfs", "server": "10.0.0.1", "path": 123},
        {"type": "hostpath", "path": True},
        {"type": "hostpath", "path": "/mnt/x", "host_path_type": 1},
    ]
    for entry in cases:
        with pytest.raises(ValueError, match="non-empty string"):
            is_storage_enabled(_uc(storage=[entry]))


def test_pvc_size_must_be_a_string_quantity_with_unit():
    """A bare number is BYTES to Kubernetes (200 -> a 200-byte PVC provisioned silently)."""
    for bad in (200, 0.5, "200"):
        with pytest.raises(ValueError, match="no unit"):
            is_storage_enabled(_uc(storage=[{"type": "pvc", "storage_class_name": "sc", "size": bad}]))
    for bad in (True, None, ""):
        with pytest.raises(ValueError, match="non-empty string"):
            is_storage_enabled(_uc(storage=[{"type": "pvc", "storage_class_name": "sc", "size": bad}]))


def test_existing_claim_rejects_falsy_provisioning_keys():
    """Conflict detection is by key presence: even null/""/0 provisioning values are stated
    intent that claim_name would silently override."""
    for key, value in (("size", None), ("storage_class_name", ""), ("access_mode", None)):
        with pytest.raises(ValueError, match="no effect"):
            is_storage_enabled(_uc(storage=[{"type": "pvc", "claim_name": "shared", key: value}]))


def test_dshm_size_must_be_a_string_quantity():
    """Container types would be stringified into garbage like "['128Gi']"."""
    for bad in (["128Gi"], {"size": "128Gi"}):
        with pytest.raises(ValueError, match="string quantity"):
            apply_dshm_size({"volumes": []}, _uc(dshm=bad))


def test_mount_path_must_be_a_non_empty_string():
    # None covers an explicit `"mount_path": null` in JSON — present key, no usable value
    for bad in ("", "   ", 123, None):
        with pytest.raises(ValueError, match="non-empty"):
            is_storage_enabled(_uc(storage=[{"type": "pvc", "storage_class_name": "sc", "mount_path": bad}]))


def test_entries_can_be_precomputed_and_passed_through():
    """Callers fetch entries once via get_storage_entries and pass them to skip re-validation."""
    uc = _uc(storage=[{"type": "pvc", "storage_class_name": "nfs-sc", "mount_path": "/mnt/ucm"}])
    entries = get_storage_entries(uc)
    assert len(entries) == 1

    docs = build_storage_pvc_docs(PVC_TEMPLATE, uc, entries)
    assert docs == build_storage_pvc_docs(PVC_TEMPLATE, uc)

    pod_a, cont_a, pod_b, cont_b = {"volumes": []}, {}, {"volumes": []}, {}
    apply_storage_volumes(pod_a, cont_a, uc, entries)
    apply_storage_volumes(pod_b, cont_b, uc)
    assert pod_a == pod_b
    assert cont_a == cont_b


def test_build_pvc_docs_missing_template_fails_clearly():
    uc = _uc(storage=[{"type": "pvc", "storage_class_name": "sc"}])
    with pytest.raises(FileNotFoundError, match="Storage PVC template not found"):
        build_storage_pvc_docs("/nonexistent/storage_pvc_template.yaml", uc)


def test_entry_type_is_required():
    """Every entry must declare its type explicitly — no inference."""
    with pytest.raises(ValueError):
        is_storage_enabled(_uc(storage=[{"storage_class_name": "sc"}]))
    with pytest.raises(ValueError):
        is_storage_enabled(_uc(storage=[{"server": "10.0.0.1", "path": "/kv"}]))


def test_build_pvc_docs_single_and_defaults():
    docs = build_storage_pvc_docs(PVC_TEMPLATE, _uc(storage=[{"type": "pvc", "storage_class_name": "nfs-sc"}]))
    assert len(docs) == 1
    pvc = docs[0]
    assert pvc["kind"] == "PersistentVolumeClaim"
    assert pvc["metadata"]["name"] == "mindie-motor-store-0"
    assert pvc["metadata"]["namespace"] == "mm"
    assert pvc["spec"]["storageClassName"] == "nfs-sc"
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "200Gi"


def test_build_pvc_docs_multi_entries_auto_named():
    """PVC/volume names are always auto-derived from the entry index."""
    uc = _uc(
        storage=[
            {"type": "pvc", "storage_class_name": "nfs-sc", "size": "512Gi", "mount_path": "/mnt/ucm"},
            {
                "type": "pvc",
                "storage_class_name": "ssd-sc",
                "access_mode": "ReadWriteOnce",
                "size": "1Ti",
                "mount_path": "/mnt/x",
            },
        ]
    )
    docs = build_storage_pvc_docs(PVC_TEMPLATE, uc)
    assert [d["metadata"]["name"] for d in docs] == ["mindie-motor-store-0", "mindie-motor-store-1"]
    assert docs[0]["spec"]["resources"]["requests"]["storage"] == "512Gi"
    assert docs[1]["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert docs[1]["spec"]["resources"]["requests"]["storage"] == "1Ti"


def test_build_pvc_docs_requires_storage_class_or_claim_name():
    with pytest.raises(ValueError):
        build_storage_pvc_docs(PVC_TEMPLATE, _uc(storage=[{"type": "pvc", "size": "10Gi"}]))


def test_existing_claim_mounts_without_generating_pvc():
    """claim_name mounts a pre-existing PVC as-is; no PVC object is generated."""
    uc = _uc(storage=[{"type": "pvc", "claim_name": "my-shared-pvc", "mount_path": "/mnt/ucm"}])
    assert is_storage_enabled(uc) is True
    assert build_storage_pvc_docs(PVC_TEMPLATE, uc) == []

    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    assert pod_spec["volumes"] == [
        {"name": "mindie-motor-store-0", "persistentVolumeClaim": {"claimName": "my-shared-pvc"}}
    ]
    assert container["volumeMounts"] == [{"name": "mindie-motor-store-0", "mountPath": "/mnt/ucm"}]


def test_existing_claim_rejects_provisioning_keys():
    """claim_name mounts the claim as-is — provisioning keys would be silently ignored."""
    for extra in ({"storage_class_name": "sc"}, {"size": "10Gi"}, {"access_mode": "ReadWriteOnce"}):
        entry = {"type": "pvc", "claim_name": "my-shared-pvc", **extra}
        with pytest.raises(ValueError):
            is_storage_enabled(_uc(storage=[entry]))


def test_claim_name_rejected_on_non_pvc_types():
    """claim_name on nfs/hostpath would be silently ignored — reject loudly instead."""
    nfs = {"type": "nfs", "server": "10.0.0.1", "path": "/kv", "claim_name": "my-pvc"}
    hostpath = {"type": "hostpath", "path": "/data", "claim_name": "my-pvc"}
    for entry in (nfs, hostpath):
        with pytest.raises(ValueError):
            is_storage_enabled(_uc(storage=[entry]))
    # an unknown type still reports the type error, claim_name or not
    with pytest.raises(ValueError, match="not supported"):
        is_storage_enabled(_uc(storage=[{"type": "cephfs", "claim_name": "my-pvc"}]))


def test_mixed_new_and_existing_pvc_entries():
    """A provisioned entry and an existing-claim entry coexist: one PVC doc, two pod volumes."""
    uc = _uc(
        storage=[
            {"type": "pvc", "storage_class_name": "csi-sc", "mount_path": "/mnt/new"},
            {"type": "pvc", "claim_name": "legacy-pvc", "mount_path": "/mnt/old"},
        ]
    )
    docs = build_storage_pvc_docs(PVC_TEMPLATE, uc)
    assert [d["metadata"]["name"] for d in docs] == ["mindie-motor-store-0"]

    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    claims = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod_spec["volumes"]}
    assert claims == {"mindie-motor-store-0": "mindie-motor-store-0", "mindie-motor-store-1": "legacy-pvc"}


def test_same_existing_claim_mounted_twice():
    """Two entries may reference the same claim (e.g. rw + ro at different paths)."""
    uc = _uc(
        storage=[
            {"type": "pvc", "claim_name": "shared", "mount_path": "/mnt/a"},
            {"type": "pvc", "claim_name": "shared", "mount_path": "/mnt/b", "read_only": True},
        ]
    )
    assert build_storage_pvc_docs(PVC_TEMPLATE, uc) == []
    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    assert [v["persistentVolumeClaim"]["claimName"] for v in pod_spec["volumes"]] == ["shared", "shared"]
    assert container["volumeMounts"] == [
        {"name": "mindie-motor-store-0", "mountPath": "/mnt/a"},
        {"name": "mindie-motor-store-1", "mountPath": "/mnt/b", "readOnly": True},
    ]


def test_apply_storage_volumes_mounts_all_and_idempotent():
    uc = _uc(
        storage=[
            {"type": "pvc", "storage_class_name": "nfs-sc", "mount_path": "/mnt/ucm"},
            {"type": "pvc", "storage_class_name": "ssd-sc", "mount_path": "/mnt/scratch"},
        ]
    )
    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    apply_storage_volumes(pod_spec, container, uc)  # idempotent
    vols = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod_spec["volumes"]}
    assert vols == {"mindie-motor-store-0": "mindie-motor-store-0", "mindie-motor-store-1": "mindie-motor-store-1"}
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts == {"mindie-motor-store-0": "/mnt/ucm", "mindie-motor-store-1": "/mnt/scratch"}


def test_apply_storage_volumes_noop_when_disabled():
    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, _uc())
    assert pod_spec["volumes"] == []
    assert "volumeMounts" not in container


def test_duplicate_mount_path_rejected():
    uc = _uc(
        storage=[
            {"type": "pvc", "storage_class_name": "a", "mount_path": "/mnt/x"},
            {"type": "nfs", "server": "10.0.0.1", "path": "/kv", "mount_path": "/mnt/x"},
        ]
    )
    with pytest.raises(ValueError):
        is_storage_enabled(uc)


def test_nfs_entry_mounts_directly_without_pvc():
    """type=nfs mounts the export as a k8s-native nfs volume; no PVC object is generated."""
    uc = _uc(storage=[{"type": "nfs", "server": "192.168.10.100", "path": "/export/ucm", "mount_path": "/mnt/ucm"}])
    assert is_storage_enabled(uc) is True
    assert build_storage_pvc_docs(PVC_TEMPLATE, uc) == []

    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    assert pod_spec["volumes"] == [
        {"name": "mindie-motor-store-0", "nfs": {"server": "192.168.10.100", "path": "/export/ucm"}}
    ]
    assert container["volumeMounts"] == [{"name": "mindie-motor-store-0", "mountPath": "/mnt/ucm"}]


def test_read_only_applies_on_the_volume_mount():
    """read_only lands on the volumeMount (valid for every type; hostPath sources have none)."""
    uc = _uc(storage=[{"type": "nfs", "server": "10.0.0.1", "path": "/kv", "mount_path": "/mnt/kv", "read_only": True}])
    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    assert pod_spec["volumes"] == [{"name": "mindie-motor-store-0", "nfs": {"server": "10.0.0.1", "path": "/kv"}}]
    assert container["volumeMounts"] == [{"name": "mindie-motor-store-0", "mountPath": "/mnt/kv", "readOnly": True}]


def test_read_only_false_leaves_mount_writable():
    """read_only: false must NOT set readOnly on the mount."""
    uc = _uc(
        storage=[{"type": "nfs", "server": "10.0.0.1", "path": "/kv", "mount_path": "/mnt/kv", "read_only": False}]
    )
    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    assert container["volumeMounts"] == [{"name": "mindie-motor-store-0", "mountPath": "/mnt/kv"}]


def test_hostpath_entry_mounts_node_dir_without_pvc():
    """type=hostpath mounts a node-local dir; no PVC object is generated."""
    uc = _uc(storage=[{"type": "hostpath", "path": "/mnt/nfs/ucm", "mount_path": "/mnt/ucm"}])
    assert build_storage_pvc_docs(PVC_TEMPLATE, uc) == []

    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    assert pod_spec["volumes"] == [{"name": "mindie-motor-store-0", "hostPath": {"path": "/mnt/nfs/ucm"}}]
    assert container["volumeMounts"] == [{"name": "mindie-motor-store-0", "mountPath": "/mnt/ucm"}]


def test_hostpath_optional_type_and_required_path():
    uc = _uc(
        storage=[
            {"type": "hostpath", "path": "/data/ucm", "mount_path": "/mnt/ucm", "host_path_type": "DirectoryOrCreate"}
        ]
    )
    pod_spec = {"volumes": []}
    apply_storage_volumes(pod_spec, {}, uc)
    assert pod_spec["volumes"][0]["hostPath"] == {"path": "/data/ucm", "type": "DirectoryOrCreate"}

    with pytest.raises(ValueError):
        is_storage_enabled(_uc(storage=[{"type": "hostpath", "mount_path": "/mnt/ucm"}]))


def test_mixed_pvc_and_nfs_entries():
    """pvc + nfs entries coexist: one PVC doc, two pod volumes of different kinds."""
    uc = _uc(
        storage=[
            {"type": "pvc", "storage_class_name": "csi-sc", "mount_path": "/mnt/ucm"},
            {"type": "nfs", "server": "10.0.0.1", "path": "/scratch", "mount_path": "/mnt/scratch"},
        ]
    )
    docs = build_storage_pvc_docs(PVC_TEMPLATE, uc)
    assert [d["metadata"]["name"] for d in docs] == ["mindie-motor-store-0"]

    pod_spec = {"volumes": []}
    container = {}
    apply_storage_volumes(pod_spec, container, uc)
    kinds = {v["name"]: ("nfs" if "nfs" in v else "pvc") for v in pod_spec["volumes"]}
    assert kinds == {"mindie-motor-store-0": "pvc", "mindie-motor-store-1": "nfs"}


def test_nfs_entry_requires_server_and_path():
    with pytest.raises(ValueError):
        is_storage_enabled(_uc(storage=[{"type": "nfs", "server": "10.0.0.1"}]))
    with pytest.raises(ValueError):
        is_storage_enabled(_uc(storage=[{"type": "nfs", "path": "/export"}]))


def test_unknown_storage_type_rejected():
    with pytest.raises(ValueError):
        is_storage_enabled(_uc(storage=[{"type": "cephfs", "mount_path": "/mnt/x"}]))


def test_apply_dshm_size_sets_and_noop():
    pod_spec = {"volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "4Gi"}}]}
    apply_dshm_size(pod_spec, _uc(dshm="128Gi"))
    assert pod_spec["volumes"][0]["emptyDir"]["sizeLimit"] == "128Gi"

    unchanged = {"volumes": [{"name": "dshm", "emptyDir": {"sizeLimit": "4Gi"}}]}
    apply_dshm_size(unchanged, _uc())
    assert unchanged["volumes"][0]["emptyDir"]["sizeLimit"] == "4Gi"


def test_apply_dshm_size_rejects_unitless():
    pod_spec = {"volumes": [{"name": "dshm", "emptyDir": {"sizeLimit": "4Gi"}}]}
    with pytest.raises(ValueError):
        apply_dshm_size(pod_spec, _uc(dshm=128))
    with pytest.raises(ValueError):
        apply_dshm_size(pod_spec, _uc(dshm="128"))
    # 0 and unitless floats are numbers too — rejected loudly, not silently skipped
    with pytest.raises(ValueError):
        apply_dshm_size(pod_spec, _uc(dshm=0))
    with pytest.raises(ValueError):
        apply_dshm_size(pod_spec, _uc(dshm=0.0))
    with pytest.raises(ValueError):
        apply_dshm_size(pod_spec, _uc(dshm="128.5"))
    # true is not a size
    with pytest.raises(ValueError):
        apply_dshm_size(pod_spec, _uc(dshm=True))
    # empty string and false both mean "keep the template default"
    apply_dshm_size(pod_spec, _uc(dshm=""))
    apply_dshm_size(pod_spec, _uc(dshm=False))
    assert pod_spec["volumes"][0]["emptyDir"]["sizeLimit"] == "4Gi"


def test_apply_dshm_size_warns_when_no_dshm_volume(caplog):
    """dshm_size set but no dshm emptyDir in the pod spec: warn instead of silently ignoring."""
    pod_spec = {"volumes": [{"name": "other", "emptyDir": {}}]}
    with caplog.at_level(logging.WARNING):
        apply_dshm_size(pod_spec, _uc(dshm="128Gi"))
    assert pod_spec["volumes"] == [{"name": "other", "emptyDir": {}}]
    assert any("dshm" in record.message for record in caplog.records)
