# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Engine pod storage.

Two independent, optional capabilities driven by ``motor_deploy_config``:

* ``storage`` — a LIST of volume specs mounted into every engine pod. Each entry MUST declare
  its ``type``:
    - ``"pvc"`` — a PVC, in one of two mutually exclusive modes: set ``storage_class_name``
      to dynamically provision a new claim (needs a CSI/provisioner StorageClass,
      ReadWriteMany for cross-node sharing; one PVC object is generated per entry), or set
      ``claim_name`` to mount an already-existing claim as-is (no PVC object is generated;
      the claim must already exist in the deploy namespace).
    - ``"nfs"`` — a k8s-native NFS volume (``server`` + ``path``) mounted directly into the
      pod. No PVC, no StorageClass, no provisioner required — only the NFS export and an nfs
      client on the nodes. All pods hitting the same server/path share the data natively.
    - ``"hostpath"`` — a node-local hostPath (``path``). Cross-node sharing ONLY if that path
      is itself a shared filesystem mounted identically on every node (hostPath-over-NFS);
      a plain local dir silently breaks cross-node prefix-cache hits.
  UCM ``storage_backends`` is the first consumer; no type is UCM-specific.
* ``dshm_size`` — raises the ``/dev/shm`` emptyDir sizeLimit (UCM's CacheStore stages there,
  default 256GiB per DP instance for MLA models, while the templates ship only 4Gi).
"""

import copy
import os
import re

import lib.constant as C
from lib.utils import load_yaml, write_yaml, logger
from lib.generator import k8s_utils


def _entry_type(entry):
    return entry.get(C.STORAGE_TYPE)


def _entry_volume_name(index):
    """Pod volume name (and, for dynamically-provisioned pvc entries, the PVC object name) —
    always index-derived, so names are deterministic across deploys and can never collide.
    Entries with ``claim_name`` mount that existing claim instead; only their volume name
    comes from here.
    """
    return f"{C.DEFAULT_STORAGE_NAME}-{index}"


def _entry_mount_path(entry, index):
    return entry.get(C.STORAGE_MOUNT_PATH, f"{C.DEFAULT_STORAGE_MOUNT_PATH}-{index}")


def _validate_mount_path(entry, index):
    # Key-presence check, not a None check: an explicit `"mount_path": null` would otherwise
    # bypass both this validation and the .get() default, emitting `mountPath: null`.
    if C.STORAGE_MOUNT_PATH not in entry:
        return
    mount_path = entry[C.STORAGE_MOUNT_PATH]
    if not isinstance(mount_path, str) or not mount_path.strip():
        raise ValueError(
            f"'{C.STORAGE}[{index}].{C.STORAGE_MOUNT_PATH}' = {mount_path!r} must be a "
            f"non-empty path string; omit the key to use the default "
            f"'{C.DEFAULT_STORAGE_MOUNT_PATH}-<i>'."
        )


def _require_non_empty_string(entry, index, key):
    """The key is present: its value must be a non-empty string. Anything else (null, true,
    numbers, "") would be emitted verbatim into the manifest and rejected — cryptically —
    only at kubectl apply time.
    """
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{C.STORAGE}[{index}].{key}' = {value!r} must be a non-empty string.")


def _validate_optional_string(entry, index, key):
    if key in entry:
        _require_non_empty_string(entry, index, key)


def _validate_read_only(entry, index):
    # Key-presence check like mount_path: an explicit `"read_only": null` must not slip
    # through as "absent". Truthiness would mount volumes read-only for strings like
    # "false"/"no"; require a real JSON boolean so intent is never guessed.
    if C.STORAGE_READ_ONLY not in entry:
        return
    read_only = entry[C.STORAGE_READ_ONLY]
    if not isinstance(read_only, bool):
        raise ValueError(
            f"'{C.STORAGE}[{index}].{C.STORAGE_READ_ONLY}' = {read_only!r} must be a JSON "
            'boolean (true/false without quotes); e.g. "false" (a string) would silently '
            "mount the volume read-only."
        )


def _is_unitless_number(value):
    """True for a bare number or digit-only string ("200", 0.5) — BYTES to Kubernetes, never
    intended at these sizes. Booleans are ints in Python but are type errors, not sizes.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip()) is not None


def _validate_pvc_size(entry, index):
    if C.STORAGE_SIZE not in entry:
        return
    size = entry[C.STORAGE_SIZE]
    # Same guard as dshm_size: a 200-byte PVC would be provisioned successfully with no
    # error anywhere.
    if _is_unitless_number(size):
        raise ValueError(
            f"'{C.STORAGE}[{index}].{C.STORAGE_SIZE}' = {size!r} has no unit (Kubernetes reads "
            f"it as bytes); did you mean '{size}Gi'? Use a quantity like '512Gi'."
        )
    _require_non_empty_string(entry, index, C.STORAGE_SIZE)


def _validate_pvc_entry(entry, index):
    _validate_optional_string(entry, index, C.STORAGE_CLAIM_NAME)
    if C.STORAGE_CLAIM_NAME in entry:
        # An existing claim is mounted as-is: its size/class/modes are fixed cluster-side,
        # so provisioning keys would be silently ignored — reject them loudly instead.
        # Key presence, not truthiness: even a falsy value ("", 0, null) is stated intent.
        provisioning_keys = [key for key in (C.STORAGE_CLASS, C.STORAGE_SIZE, C.STORAGE_ACCESS_MODE) if key in entry]
        if provisioning_keys:
            raise ValueError(
                f"'{C.STORAGE}[{index}]' sets both '{C.STORAGE_CLAIM_NAME}' and "
                f"{provisioning_keys}: '{C.STORAGE_CLAIM_NAME}' mounts an existing PVC "
                "as-is, so provisioning keys have no effect. Drop them, or drop "
                f"'{C.STORAGE_CLAIM_NAME}' to provision a new claim."
            )
        return
    if C.STORAGE_CLASS not in entry:
        raise ValueError(
            f"'{C.STORAGE}[{index}]' (pvc) needs '{C.STORAGE_CLASS}' or '{C.STORAGE_CLAIM_NAME}'. "
            f"Set '{C.STORAGE_CLASS}' to a StorageClass that supports dynamic provisioning "
            "(ReadWriteMany when Prefill/Decode run on different nodes) to create a new "
            f"claim, set '{C.STORAGE_CLAIM_NAME}' to mount a PVC that already exists in the "
            "deploy namespace, or use type 'nfs' with server/path to mount an NFS export "
            "directly without a provisioner."
        )
    _require_non_empty_string(entry, index, C.STORAGE_CLASS)
    _validate_pvc_size(entry, index)
    _validate_optional_string(entry, index, C.STORAGE_ACCESS_MODE)


def _validate_nfs_entry(entry, index):
    if C.NFS_SERVER not in entry or C.PATH not in entry:
        raise ValueError(
            f"'{C.STORAGE}[{index}]' with type 'nfs' requires both '{C.NFS_SERVER}' and "
            f"'{C.PATH}' (the NFS export address and exported directory)."
        )
    _require_non_empty_string(entry, index, C.NFS_SERVER)
    _require_non_empty_string(entry, index, C.PATH)


def _validate_hostpath_entry(entry, index):
    if C.PATH not in entry:
        raise ValueError(
            f"'{C.STORAGE}[{index}]' with type 'hostpath' requires '{C.PATH}' (the directory on the node to mount)."
        )
    _require_non_empty_string(entry, index, C.PATH)
    _validate_optional_string(entry, index, C.STORAGE_HOST_PATH_TYPE)


_TYPE_VALIDATORS = {
    C.STORAGE_TYPE_PVC: _validate_pvc_entry,
    C.STORAGE_TYPE_NFS: _validate_nfs_entry,
    C.STORAGE_TYPE_HOSTPATH: _validate_hostpath_entry,
}


def _validate_entry(entry, index):
    _validate_mount_path(entry, index)
    _validate_read_only(entry, index)
    entry_type = _entry_type(entry)
    if entry_type is None:
        raise ValueError(
            f"'{C.STORAGE}[{index}].{C.STORAGE_TYPE}' is required; use '{C.STORAGE_TYPE_PVC}' "
            "(a PVC — dynamically provisioned via storage_class_name, or an existing claim via "
            f"claim_name), '{C.STORAGE_TYPE_NFS}' (direct NFS mount) or "
            f"'{C.STORAGE_TYPE_HOSTPATH}' (node-local directory)."
        )
    validator = _TYPE_VALIDATORS.get(entry_type)
    if validator is None:
        raise ValueError(
            f"'{C.STORAGE}[{index}].{C.STORAGE_TYPE}' = '{entry_type}' is not supported; "
            f"use '{C.STORAGE_TYPE_PVC}', '{C.STORAGE_TYPE_NFS}' or '{C.STORAGE_TYPE_HOSTPATH}'."
        )
    validator(entry, index)
    if entry_type != C.STORAGE_TYPE_PVC and entry.get(C.STORAGE_CLAIM_NAME):
        raise ValueError(
            f"'{C.STORAGE}[{index}].{C.STORAGE_CLAIM_NAME}' only applies to type "
            f"'{C.STORAGE_TYPE_PVC}' — a '{entry_type}' entry would silently ignore it and "
            f"never mount the claim. Use type '{C.STORAGE_TYPE_PVC}' with "
            f"'{C.STORAGE_CLAIM_NAME}' to mount the existing PVC."
        )


def get_storage_entries(user_config):
    """Return the validated, enabled storage specs from motor_deploy_config.storage (a list).

    A single dict is tolerated and treated as a one-element list. Every entry is validated
    (explicit type + type-specific required keys); volume/PVC names are auto-derived from the
    entry index so they can never collide, leaving only mount_path uniqueness to check.

    Callers invoking several storage functions on the same config should fetch this once and
    pass it via their ``entries`` parameter, so validation is not repeated across those calls.
    """
    raw = user_config.get(C.MOTOR_DEPLOY_CONFIG, {}).get(C.STORAGE)
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(
            f"'{C.STORAGE}' = {raw!r} must be a list of volume specs (or a single dict); "
            "a value of any other type would silently mount nothing."
        )
    entries = []
    for raw_index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"'{C.STORAGE}[{raw_index}]' = {entry!r} must be an object with a "
                f"'{C.STORAGE_TYPE}' key; skipping it would silently leave the volume unmounted."
            )
        enable = entry.get(C.STORAGE_ENABLE, True)
        # Truthiness would silently enable strings like "false"/"no"; require a real
        # JSON boolean so intent is never guessed.
        if not isinstance(enable, bool):
            raise ValueError(
                f"'{C.STORAGE}[{raw_index}].{C.STORAGE_ENABLE}' = {enable!r} must be a JSON "
                'boolean (true/false without quotes); e.g. "false" (a string) would '
                "silently enable the volume."
            )
        if enable:
            entries.append(entry)

    seen_mounts = {}
    for index, entry in enumerate(entries):
        _validate_entry(entry, index)
        mount = _entry_mount_path(entry, index)
        if mount in seen_mounts:
            raise ValueError(
                f"'{C.STORAGE}[{index}]' resolves to duplicate mount_path '{mount}' (also "
                f"'{C.STORAGE}[{seen_mounts[mount]}]'); give each entry a distinct mount_path."
            )
        seen_mounts[mount] = index
    return entries


def is_storage_enabled(user_config):
    """True when at least one enabled storage entry is configured."""
    return bool(get_storage_entries(user_config))


def build_storage_pvc_docs(input_yaml, user_config, entries=None):
    """Render one PVC dict per enabled dynamically-provisioned pvc entry.

    nfs/hostpath entries and pvc entries with ``claim_name`` (pre-existing claims) need no
    k8s object. Pass ``entries`` (from ``get_storage_entries``) to skip re-validation.
    """
    if entries is None:
        entries = get_storage_entries(user_config)
    if not entries:
        return []
    if not os.path.exists(input_yaml):
        raise FileNotFoundError(
            f"Storage PVC template not found: {input_yaml}. Please ensure "
            "storage_pvc_template.yaml exists in the yaml_template folder."
        )
    namespace = user_config[C.MOTOR_DEPLOY_CONFIG][C.CONFIG_JOB_ID]
    template = load_yaml(input_yaml, False)[0]

    docs = []
    for index, entry in enumerate(entries):
        if _entry_type(entry) != C.STORAGE_TYPE_PVC or entry.get(C.STORAGE_CLAIM_NAME):
            continue
        pvc = copy.deepcopy(template)
        pvc[C.METADATA][C.NAMESPACE] = namespace
        pvc[C.METADATA][C.NAME] = _entry_volume_name(index)
        spec = pvc[C.SPEC]
        spec[C.STORAGE_CLASS_NAME] = entry[C.STORAGE_CLASS]
        spec[C.ACCESS_MODES] = [entry.get(C.STORAGE_ACCESS_MODE, C.DEFAULT_STORAGE_ACCESS_MODE)]
        # the k8s field spec.resources.requests.storage shares the "storage" literal with the config key
        spec[C.RESOURCES][C.REQUESTS][C.STORAGE] = entry.get(C.STORAGE_SIZE, C.DEFAULT_STORAGE_SIZE)
        docs.append(pvc)
    return docs


def generate_yaml_storage_pvc(input_yaml, output_file, user_config, entries=None):
    """Render all dynamically-provisioned pvc entries into one (multi-document) file queued
    for ``kubectl apply``.

    No-op when no entry needs a cluster-side object (only nfs/hostpath entries, or pvc
    entries mounting an existing claim via ``claim_name``).
    """
    docs = build_storage_pvc_docs(input_yaml, user_config, entries)
    if not docs:
        return
    logger.info(f"Generating {len(docs)} storage PVC(s) from {input_yaml} to {output_file}")
    write_yaml(docs, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)


def _build_volume(entry, index):
    name = _entry_volume_name(index)
    entry_type = _entry_type(entry)
    if entry_type == C.STORAGE_TYPE_NFS:
        return {C.NAME: name, C.STORAGE_TYPE_NFS: {C.NFS_SERVER: entry[C.NFS_SERVER], C.PATH: entry[C.PATH]}}
    if entry_type == C.STORAGE_TYPE_HOSTPATH:
        host_path_source = {C.PATH: entry[C.PATH]}
        if entry.get(C.STORAGE_HOST_PATH_TYPE):
            # the k8s hostPath.type field shares the "type" literal with the config entry key
            host_path_source[C.STORAGE_TYPE] = entry[C.STORAGE_HOST_PATH_TYPE]
        return {C.NAME: name, C.HOST_PATH: host_path_source}
    claim_name = entry.get(C.STORAGE_CLAIM_NAME) or name
    return {C.NAME: name, C.PERSISTENT_VOLUME_CLAIM: {C.CLAIM_NAME: claim_name}}


def apply_storage_volumes(pod_spec, container, user_config, entries=None):
    """Mount every enabled storage entry into a pod. Idempotent; no-op when empty.

    pvc entries mount by claimName; nfs entries mount the export directly; hostpath entries
    mount a node directory. ``read_only`` applies at the volumeMount level for every type.
    Operates on a raw pod spec + container so the Deployment, InferServiceSet and
    single-container paths all reuse it. Pass ``entries`` (from ``get_storage_entries``) to
    skip re-validation.
    """
    if entries is None:
        entries = get_storage_entries(user_config)
    if not entries:
        return
    volumes = pod_spec.setdefault(C.VOLUMES, [])
    mounts = container.setdefault(C.VOLUME_MOUNTS, [])
    for index, entry in enumerate(entries):
        name = _entry_volume_name(index)
        mount_path = _entry_mount_path(entry, index)
        if not any(volume.get(C.NAME) == name for volume in volumes):
            volumes.append(_build_volume(entry, index))
        if not any(mount.get(C.NAME) == name for mount in mounts):
            mount = {C.NAME: name, C.MOUNT_PATH: mount_path}
            if entry.get(C.STORAGE_READ_ONLY):
                mount[C.K8S_READ_ONLY] = True
            mounts.append(mount)
    logger.info("Applied %d storage volume(s)", len(entries))


def apply_dshm_size(pod_spec, user_config):
    """Raise the /dev/shm emptyDir sizeLimit from motor_deploy_config.dshm_size, if set.

    Independent of the storage volumes — settable on its own.
    """
    dshm_size = user_config.get(C.MOTOR_DEPLOY_CONFIG, {}).get(C.DSHM_SIZE)
    # None/""/false all mean "leave the template default"; true is not a size.
    if dshm_size is None or dshm_size == "" or dshm_size is False:
        return
    if dshm_size is True:
        raise ValueError(
            f"'{C.DSHM_SIZE}' = true is not a size; use a quantity like '128Gi', or "
            "false/omit to keep the template default."
        )
    # A bare number is bytes to Kubernetes (128 -> 128 bytes), almost never intended for
    # /dev/shm at these sizes (and 0 disables nothing meaningfully). Reject it loudly
    # instead of silently under-provisioning.
    if _is_unitless_number(dshm_size):
        raise ValueError(
            f"'{C.DSHM_SIZE}' = '{dshm_size}' has no unit (Kubernetes reads it as bytes); "
            f"did you mean '{dshm_size}Gi'? Use a quantity like '128Gi'."
        )
    # Container types (list/dict) would be stringified into garbage like "['128Gi']".
    if not isinstance(dshm_size, str):
        raise ValueError(f"'{C.DSHM_SIZE}' = {dshm_size!r} must be a string quantity like '128Gi'.")
    for volume in pod_spec.get(C.VOLUMES, []):
        if volume.get(C.NAME) == C.DSHM_VOLUME and C.EMPTY_DIR in volume:
            volume[C.EMPTY_DIR][C.SIZE_LIMIT] = str(dshm_size)
            logger.info("Set /dev/shm sizeLimit=%s", dshm_size)
            return
    logger.warning(
        "'%s' = '%s' is set but the pod spec has no '%s' emptyDir volume; nothing was resized",
        C.DSHM_SIZE,
        dshm_size,
        C.DSHM_VOLUME,
    )
