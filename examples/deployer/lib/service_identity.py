# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Service identity: (namespace, serviceN) identifies one complete Motor service.

On the default ``infer_service_set`` mode the deployer assigns ``service1``, ``service2``,
... from the pods and ConfigMaps already in that namespace. The number is one more than
the highest one still present there, and each namespace has its own sequence. There is
no ``user_config`` field for it.

The identity is prefixed onto the InferServiceSet name, which is where every CRD-derived
Pod and Service name comes from. The template root (``vllm``, or whatever the template
already says) stays in the name. Names the CRD does not derive (ConfigMap, ServiceAccount,
PVC) are suffixed with the identity. Pods also carry ownership labels, because a name is
not an ownership test: the prefix "a" also matches "ab".

``multi_deployment`` and ``single_container`` keep their historical names. Their pods are
not derived from an InferServiceSet, so this identity is not applied there.

The active identity is module state, set once at the deployer entry, the same way
``g_engine_base_name`` and ``g_user_config_path`` already work. Keeping it here means the
generators do not have to thread it through every naming helper.
"""

from __future__ import annotations

import logging
import os
import re

import lib.constant as C

logger = logging.getLogger(__name__)

# DNS-1123 label: lowercase alphanumerics and '-', starting and ending alphanumeric.
_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# Template literals naming a service-dedicated component. Engine roles are absent on
# purpose: their app label derives from the InferServiceSet name, which already carries
# the identity.
_OWNED_COMPONENTS = frozenset(
    {
        "mindie-motor-controller",
        "mindie-motor-coordinator",
        "mindie-motor-kv-store",
        "mindie-motor-kv-conductor",
        "mindie-motor-mf-store",
    }
)

# NodePort is cluster-wide, so two services in one namespace cannot share one.
_REQUIRED_NODE_PORTS = (
    C.COORDINATOR_INFER_NODE_PORT,
    C.COORDINATOR_OBS_NODE_PORT,
    C.CONTROLLER_OBSERVABILITY_NODE_PORT,
)

_SERVICE_PREFIX = "service"
# service1, service12. A trailing hyphen starts the CRD's instance index, not another number.
_SERVICE_NUMBER = re.compile(rf"(?:^|-){_SERVICE_PREFIX}([1-9][0-9]*)(?:-|$)")

g_service_name = ""
g_service_namespace = ""


def _namespace_of(deploy_config) -> str:
    namespace = deploy_config.get(C.CONFIG_JOB_ID)
    if isinstance(namespace, str) and namespace.strip():
        return namespace.strip()
    return "mindie-motor"


def _missing_namespace(exc: BaseException) -> bool:
    return "NotFound" in str(exc)


def _collect_numbers(text: str, used: set[int]) -> None:
    for column in text.split():
        if column == "<none>":
            continue
        for match in _SERVICE_NUMBER.finditer(column):
            used.add(int(match.group(1)))


def _kubectl_text(*args: str) -> str:
    from lib.generator.k8s_utils import run_cmd_get_output  # pylint: disable=import-outside-toplevel,cyclic-import

    return run_cmd_get_output(["kubectl", *args], timeout=30)


def _used_numbers(namespace: str) -> set[int]:
    """Highest assigned numbers still present in this namespace.

    Pods carry the number in the name (``service2-vllm-0-controller-...``) and on
    ``motor.mindie.io/service``. The ConfigMap ``motor-config-service2`` is created
    with the deploy, so a second launch started before the pods are Ready still
    sees the first service.
    """
    used: set[int] = set()
    try:
        pods = _kubectl_text(
            "get",
            "pods",
            "-n",
            namespace,
            "--no-headers",
            "-o",
            r"custom-columns=NAME:.metadata.name,SVC:.metadata.labels.motor\.mindie\.io/service",
        )
    except RuntimeError as exc:
        if _missing_namespace(exc):
            return used
        raise
    _collect_numbers(pods, used)
    try:
        configmaps = _kubectl_text(
            "get",
            "configmaps",
            "-n",
            namespace,
            "--no-headers",
            "-o",
            "custom-columns=NAME:.metadata.name",
        )
    except RuntimeError as exc:
        if _missing_namespace(exc):
            return used
        raise
    _collect_numbers(configmaps, used)
    return used


def _identity_name(number: int, namespace: str) -> str:
    name = f"{_SERVICE_PREFIX}{number}"
    if len(name) > C.SERVICE_NAME_MAX_LEN:
        raise ValueError(
            f"The service identity {name!r} in namespace {namespace!r} exceeds {C.SERVICE_NAME_MAX_LEN} characters"
        )
    return name


def _next_service_name(namespace: str, reuse_existing: bool) -> str:
    used = _used_numbers(namespace)
    if reuse_existing:
        if len(used) > 1:
            names = ", ".join(_identity_name(number, namespace) for number in sorted(used))
            raise ValueError(
                f"namespace {namespace} already has {names}; refresh or scale cannot tell which service to change"
            )
        number = max(used) if used else 1
    else:
        number = max(used) + 1 if used else 1
    return _identity_name(number, namespace)


def _deploy_mode(deploy_config, deploy_mode):
    if deploy_mode is not None:
        return deploy_mode
    from lib.generator.k8s_utils import get_deploy_mode_from_config  # pylint: disable=import-outside-toplevel,cyclic-import

    return get_deploy_mode_from_config(deploy_config)


def set_identity(user_config, deploy_mode=None, reuse_existing=False):
    """Assign this launch's identity. Returns '' when this deploy mode is not numbered."""
    global g_service_name, g_service_namespace
    deploy_config = (user_config or {}).get(C.MOTOR_DEPLOY_CONFIG, {})
    if not isinstance(deploy_config, dict):
        deploy_config = {}
    g_service_namespace = _namespace_of(deploy_config)
    if _deploy_mode(deploy_config, deploy_mode) != C.DEPLOY_MODE_INFER_SERVICE_SET:
        g_service_name = ""
        return g_service_name
    if deploy_config.get("service_name") not in (None, ""):
        logger.info("motor_deploy_config.service_name is ignored; the deployer assigns service1, service2, ...")
    g_service_name = _next_service_name(g_service_namespace, reuse_existing)
    logger.info("Assigned service identity %s in namespace %s", g_service_name, g_service_namespace)
    return g_service_name


def service_name() -> str:
    """Active identity, or '' when this deploy mode keeps the historical names."""
    return g_service_name


def qualify(base):
    """Append '-<service_name>' to a name that is not derived from the InferServiceSet."""
    if not g_service_name or not base:
        return base
    return f"{base}-{g_service_name}"


def infer_set_name(template_name):
    """Name of the InferServiceSet, from which the CRD derives every Pod and Service name.

    The identity is prefixed onto the template root, so pods read as
    ``service1-vllm-0-prefill-0`` and the original root stays visible.
    """
    if not g_service_name:
        return template_name
    if not template_name:
        return g_service_name
    return f"{g_service_name}-{template_name}"


def configmap_name() -> str:
    """The motor-config ConfigMap is service-dedicated: each service mounts its own config."""
    return qualify(C.MOTOR_CONFIG_CONFIGMAP_NAME)


def coordinator_app_label() -> str:
    """Value of the coordinator pods' app label; Controller discovers them by it."""
    return qualify("mindie-motor-coordinator")


def output_root() -> str:
    """Named services keep their manifests apart so uninstall acts on one service only."""
    if not g_service_name:
        return C.OUTPUT_ROOT_PATH
    return os.path.join(C.OUTPUT_ROOT_PATH, g_service_namespace or "default", g_service_name)


def _validate_node_ports(deploy_config) -> None:
    """Check NodePorts when the config sets them. Omitted ports keep the template values."""
    present = [key for key in _REQUIRED_NODE_PORTS if deploy_config.get(key) not in (None, "")]
    if not present:
        return
    missing = [key for key in _REQUIRED_NODE_PORTS if key not in present]
    if missing:
        raise ValueError(
            f"{C.MOTOR_DEPLOY_CONFIG} NodePorts {list(_REQUIRED_NODE_PORTS)} must all be set together; "
            f"missing: {sorted(missing)}"
        )
    try:
        ports = {key: int(deploy_config[key]) for key in _REQUIRED_NODE_PORTS}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{C.MOTOR_DEPLOY_CONFIG} NodePorts {list(_REQUIRED_NODE_PORTS)} must be integers") from exc
    for key, port in ports.items():
        if not C.NODEPORT_RANGE_MIN <= port <= C.NODEPORT_RANGE_MAX:
            raise ValueError(
                f"{C.MOTOR_DEPLOY_CONFIG}.{key}={port} is outside [{C.NODEPORT_RANGE_MIN}, {C.NODEPORT_RANGE_MAX}]"
            )
    if len(set(ports.values())) != len(ports):
        raise ValueError(f"{C.MOTOR_DEPLOY_CONFIG} NodePorts {list(ports)} must differ from each other")


def validate(user_config, deploy_mode=None, reuse_existing=False) -> None:
    """Assign the identity and reject one that cannot yield valid resource names."""
    deploy_config = (user_config or {}).get(C.MOTOR_DEPLOY_CONFIG, {})
    if not isinstance(deploy_config, dict):
        deploy_config = {}
    service = set_identity(user_config, deploy_mode, reuse_existing)
    if not service:
        return

    # The inference entry may already be renamed per tenant. The CRD expands it into
    # "{entry}-{set}-0-coordinator", and the set keeps the template root (vllm).
    from lib.utils import get_coordinator_service_name  # pylint: disable=cyclic-import

    entry_name = f"{get_coordinator_service_name(deploy_config)}-{infer_set_name('vllm')}-0-{C.COORDINATOR}"
    if len(entry_name) > C.DNS_LABEL_MAX_LEN or not _DNS1123_LABEL.match(entry_name):
        raise ValueError(
            f"The resulting coordinator Service name must be a DNS-1123 label of at most "
            f"{C.DNS_LABEL_MAX_LEN} characters, got {entry_name!r}"
        )

    _validate_node_ports(deploy_config)


def _qualify_owned_label(labels) -> None:
    """Suffix app / deploy-name when they name a service-dedicated component."""
    if not isinstance(labels, dict):
        return
    for key in (C.APP, C.LABEL_DEPLOY_NAME):
        if labels.get(key) in _OWNED_COMPONENTS:
            labels[key] = qualify(labels[key])


def _stamp(labels) -> None:
    labels[C.LABEL_MOTOR_SERVICE] = g_service_name
    labels[C.LABEL_MOTOR_NAMESPACE] = g_service_namespace


def stamp_ownership(metadata) -> None:
    """Add the ownership labels to an object whose name is already qualified."""
    if not g_service_name or not isinstance(metadata, dict):
        return
    _stamp(metadata.setdefault(C.LABELS, {}))


def _rewrite_configmap_refs(pod_spec) -> None:
    """Point the managed motor-config volume at this service's ConfigMap."""
    for volume in pod_spec.get(C.VOLUMES) or []:
        config_map = volume.get("configMap") if isinstance(volume, dict) else None
        if isinstance(config_map, dict) and config_map.get(C.NAME) == C.MOTOR_CONFIG_CONFIGMAP_NAME:
            config_map[C.NAME] = configmap_name()


def _apply_to_pod_template(spec) -> None:
    template = spec.get(C.TEMPLATE)
    if not isinstance(template, dict):
        return
    pod_labels = template.setdefault(C.METADATA, {}).setdefault(C.LABELS, {})
    _qualify_owned_label(pod_labels)
    _stamp(pod_labels)
    pod_spec = template.get(C.SPEC)
    if isinstance(pod_spec, dict):
        _rewrite_configmap_refs(pod_spec)


def apply_to_infer_service(infer_doc) -> None:
    """Carry the identity into the InferServiceSet document.

    Renaming ``metadata.name`` is the main lever: the CRD derives the pods and every Service
    it creates from that name, and the deployer derives the scaling scope and the in-cluster
    domain names from the same value, so all of those follow.

    Must run before the roles are configured. No-op in compatible mode, and not idempotent:
    call once per freshly loaded document.
    """
    if not g_service_name or not isinstance(infer_doc, dict):
        return

    metadata = infer_doc.setdefault(C.METADATA, {})
    metadata[C.NAME] = infer_set_name(metadata.get(C.NAME, ""))
    stamp_ownership(metadata)

    for role in infer_doc.get(C.SPEC, {}).get(C.TEMPLATE, {}).get(C.ROLES) or []:
        if not isinstance(role, dict):
            continue
        # The CRD expands the role Service names with the set name, so only their selector
        # needs scoping; left alone it would also match the sibling service's pods.
        for service in role.get(C.SERVICES) or []:
            if isinstance(service, dict):
                _qualify_owned_label((service.get(C.SPEC) or {}).get(C.SELECTOR))
        workload_spec = role.get(C.SPEC)
        if isinstance(workload_spec, dict):
            _qualify_owned_label((workload_spec.get(C.SELECTOR) or {}).get(C.MATCHLABELS))
            _apply_to_pod_template(workload_spec)


def scope_rbac(rbac_resources, namespace) -> dict:
    """Scope managed RBAC without touching its rules; return the ServiceAccount renames.

    ClusterRole and ClusterRoleBinding are cluster-scoped, so their names already carry the
    namespace; with an identity they carry the service too. A '.' separates the two: neither
    a namespace nor a service name may contain one, so the boundary is unambiguous and
    ``a-b`` + ``c`` cannot collide with ``a`` + ``b-c``.
    """
    accounts = {}

    def cluster_scoped(name):
        return f"{name}-{namespace}.{g_service_name}" if g_service_name else f"{name}-{namespace}"

    for resource in rbac_resources:
        metadata = resource[C.METADATA]
        kind = resource.get(C.KIND)
        if g_service_name:
            _stamp(metadata.setdefault(C.LABELS, {}))
        if kind == C.SERVICE_ACCOUNT:
            metadata[C.NAMESPACE] = namespace
            if g_service_name:
                # Empty in the compatible mode, so retargeting is a no-op there.
                accounts[metadata[C.NAME]] = qualify(metadata[C.NAME])
                metadata[C.NAME] = accounts[metadata[C.NAME]]
        elif kind == "ClusterRole":
            metadata[C.NAME] = cluster_scoped(metadata[C.NAME])
        elif kind == C.CLUSTER_ROLE_BINDING:
            metadata[C.NAME] = cluster_scoped(metadata[C.NAME])
            role_ref = resource.get("roleRef")
            if role_ref:
                role_ref[C.NAME] = cluster_scoped(role_ref[C.NAME])
            for subject in resource.get(C.SUBJECTS) or []:
                if subject.get(C.KIND) == C.SERVICE_ACCOUNT:
                    subject[C.NAMESPACE] = namespace
                    subject[C.NAME] = qualify(subject.get(C.NAME, ""))
    return accounts


def retarget_service_accounts(node, accounts) -> None:
    """Point every workload's serviceAccountName at this service's renamed account.

    Walks the document because the InferServiceSet nests pod specs under each role.
    """
    if not accounts:
        return
    if isinstance(node, list):
        for child in node:
            retarget_service_accounts(child, accounts)
    elif isinstance(node, dict):
        current = node.get("serviceAccountName")
        if isinstance(current, str) and current in accounts:
            node["serviceAccountName"] = accounts[current]
        for child in node.values():
            if isinstance(child, (dict, list)):
                retarget_service_accounts(child, accounts)
