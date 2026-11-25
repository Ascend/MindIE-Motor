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
from copy import deepcopy
from pathlib import Path

import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[4] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.rbg import lifecycle  # noqa: E402
from lib.rbg.lifecycle import update_rbg_instance_num  # noqa: E402
from lib.rbg.scaling import (  # noqa: E402
    generate_scaling_resources,
)


def make_engine_role(name, replicas=2, size=1, base_port="30000", ports_enabled=None):
    environment = [
        {"name": "ASCEND_BASE_PORT", "value": base_port},
        {"name": "HIXL_LISTEN_PORT", "value": "16000"},
        {"name": "MOTOR_INSTANCE_ASCEND_PORT_STRIDE", "value": "4000"},
    ]
    if ports_enabled is not None:
        environment.append({"name": "MOTOR_INSTANCE_PORTS_ENABLE", "value": str(ports_enabled).lower()})
    return {
        "name": name,
        "replicas": replicas,
        "customComponentsPattern": {
            "components": [
                {
                    "name": "engine",
                    "size": size,
                    "template": {
                        "metadata": {"labels": {"infer.huawei.com/inferservice-name": "motor-0"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "engine",
                                    "env": environment,
                                }
                            ]
                        },
                    },
                }
            ]
        },
    }


def make_leader_worker_role(name, replicas=2, size=2, base_port="30000", ports_enabled=None):
    role = make_engine_role(name, replicas, size, base_port, ports_enabled)
    component = role.pop("customComponentsPattern")["components"][0]
    role["leaderWorkerPattern"] = {"size": size, "template": component["template"]}
    return role


def make_rbg(*roles, gang=True):
    return {
        "metadata": {
            "name": "motor",
            "namespace": "motor-demo",
            "annotations": {"rbg.workloads.x-k8s.io/group-gang-scheduling": str(gang).lower()},
        },
        "spec": {"roles": list(roles)},
    }


def make_user_config(**extra):
    result = {"motor_deploy_config": {"rbg_config": {}}}
    result.update(extra)
    return result


def make_current_hpa(role="prefill", *, minimum=1, maximum=4):
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": f"motor-{role}-hpa",
            "namespace": "motor-demo",
            "labels": {
                "app.kubernetes.io/managed-by": "motor-deployer",
                "rbg.workloads.x-k8s.io/group-name": "motor",
            },
        },
        "spec": {
            "minReplicas": minimum,
            "maxReplicas": maximum,
            "scaleTargetRef": {
                "apiVersion": "workloads.x-k8s.io/v1alpha2",
                "kind": "RoleBasedGroupScalingAdapter",
                "name": f"motor-{role}",
            },
            "metrics": [
                {
                    "type": "External",
                    "external": {
                        "metric": {
                            "name": f"motor_{role}_utilization",
                            "selector": {
                                "matchLabels": {
                                    "kubernetes_namespace": "motor-demo",
                                    "infer_huawei_com_inferservice_name": "motor-0",
                                }
                            },
                        },
                        "target": {"type": "Value", "value": "0.8"},
                    },
                }
            ],
        },
    }


def make_explicit_port_config(role="prefill", **extra):
    result = make_user_config(**extra)
    result[f"motor_engine_{role}_config"] = {
        "motor_nodemanger_config": {
            "port_allocator_config": {"enable": False},
            "api_config": {"node_manager_port": 20000},
            "endpoint_config": {"base_port": 21000},
        },
        "engine_config": {"data_parallel_rpc_port": 22000},
    }
    return result


def test_first_deployment_generates_default_same_name_policy_without_hpa():
    rbg = make_rbg(make_engine_role("prefill"), make_engine_role("decode", replicas=0))
    original = deepcopy(rbg)

    resources = generate_scaling_resources(make_user_config(), rbg)

    assert resources["policy"]["metadata"]["name"] == "motor"
    assert resources["policy"]["metadata"]["namespace"] == "motor-demo"
    assert resources["policy"]["spec"]["policies"] == [
        {
            "name": "engine-scaling",
            "roles": ["prefill", "decode"],
            "strategy": {"scaling": {"maxSkew": "100%", "progression": "OrderReady"}},
        }
    ]
    assert resources["hpas"] == {}
    assert resources["policy"]["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "motor-deployer",
        "rbg.workloads.x-k8s.io/group-name": "motor",
    }
    assert rbg == original


def test_update_distinguishes_omitted_configuration_from_explicit_hpa_disable():
    rbg = make_rbg(make_engine_role("prefill"))
    current = {"policy": None, "hpas": {}}

    omitted = generate_scaling_resources(make_user_config(), rbg, is_update=True, current=current)
    disabled = generate_scaling_resources(
        make_user_config(scaling_policy={}),
        rbg,
        is_update=True,
        current=current,
    )

    assert omitted == {"policy": None, "hpas": None}
    assert disabled == {"policy": None, "hpas": {}}


def test_custom_policy_defaults_scaling_fields_and_accepts_rolling_update():
    config = make_user_config()
    config["motor_deploy_config"]["rbg_config"]["coordinated_policy"] = {
        "policies": [
            {
                "name": "prefill-policy",
                "roles": ["prefill"],
                "strategy": {
                    "scaling": {},
                    "rollingUpdate": {"maxSkew": 1, "partition": "0%", "maxUnavailable": "50%"},
                },
            }
        ]
    }

    policy = generate_scaling_resources(config, make_rbg(make_engine_role("prefill"), gang=False))["policy"]

    assert policy["spec"]["policies"][0]["strategy"]["scaling"] == {
        "maxSkew": "100%",
        "progression": "OrderReady",
    }


@pytest.mark.parametrize(
    "policies, error",
    [
        (
            [
                {"name": "one", "roles": ["prefill"], "strategy": {"scaling": {}}},
                {"name": "two", "roles": ["prefill"], "strategy": {"scaling": {}}},
            ],
            "more than one",
        ),
        ([{"name": "one", "roles": ["controller"], "strategy": {"scaling": {}}}], "enabled engine role"),
        ([{"name": "one", "roles": ["prefill"], "strategy": {}}], "requires scaling or rollingUpdate"),
        (
            [{"name": "one", "roles": ["prefill"], "strategy": {"scaling": {"maxSkew": "0%"}}}],
            "must be in",
        ),
    ],
)
def test_policy_rejects_invalid_rules(policies, error):
    config = make_user_config()
    config["motor_deploy_config"]["rbg_config"]["coordinated_policy"] = {"policies": policies}

    with pytest.raises(ValueError, match=error):
        generate_scaling_resources(config, make_rbg(make_engine_role("prefill"), gang=False))


def test_policy_rejects_gang_partial_skew_and_multi_pod_order_scheduled():
    config = make_user_config()
    config["motor_deploy_config"]["rbg_config"]["coordinated_policy"] = {
        "policies": [
            {
                "name": "prefill-policy",
                "roles": ["prefill"],
                "strategy": {"scaling": {"maxSkew": "50%", "progression": "OrderReady"}},
            }
        ]
    }
    with pytest.raises(ValueError, match="group gang"):
        generate_scaling_resources(config, make_rbg(make_engine_role("prefill")))

    config["motor_deploy_config"]["rbg_config"]["coordinated_policy"]["policies"][0]["strategy"] = {
        "scaling": {"maxSkew": "100%", "progression": "OrderScheduled"}
    }
    with pytest.raises(ValueError, match="more than one pod"):
        generate_scaling_resources(config, make_rbg(make_engine_role("prefill", size=2), gang=False))


def test_hpa_targets_adapter_and_carries_metric_scope_and_behavior():
    behavior = {"scaleDown": {"stabilizationWindowSeconds": 300}}
    config = make_user_config(
        scaling_policy={
            "prefill": {
                "min_replicas": 1,
                "max_replicas": 4,
                "target": 3,
                "target_type": "AverageValue",
                "behavior": behavior,
            }
        }
    )

    hpa = generate_scaling_resources(config, make_rbg(make_engine_role("prefill")))["hpas"]["prefill"]

    assert hpa["metadata"]["name"] == "motor-prefill-hpa"
    assert hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "workloads.x-k8s.io/v1alpha2",
        "kind": "RoleBasedGroupScalingAdapter",
        "name": "motor-prefill",
    }
    assert hpa["spec"]["minReplicas"] == 1
    assert hpa["spec"]["maxReplicas"] == 4
    external = hpa["spec"]["metrics"][0]["external"]
    assert external["metric"]["name"] == "motor_prefill_utilization"
    assert external["metric"]["selector"]["matchLabels"] == {
        "kubernetes_namespace": "motor-demo",
        "infer_huawei_com_inferservice_name": "motor-0",
    }
    assert external["target"] == {"type": "AverageValue", "averageValue": "3.0"}
    assert hpa["spec"]["behavior"] == behavior
    assert hpa["spec"]["behavior"] is not behavior


def test_hpa_allows_max_replicas_above_instance_num_max():
    config = make_user_config(scaling_policy={"prefill": {"max_replicas": C.INSTANCE_NUM_MAX + 1}})

    hpa = generate_scaling_resources(config, make_rbg(make_engine_role("prefill")))["hpas"]["prefill"]

    assert hpa["spec"]["maxReplicas"] == C.INSTANCE_NUM_MAX + 1


def test_leader_worker_hpa_reads_metric_scope_from_inline_template():
    config = make_user_config(scaling_policy={"prefill": {"max_replicas": 4}})

    hpa = generate_scaling_resources(config, make_rbg(make_leader_worker_role("prefill")))["hpas"]["prefill"]

    labels = hpa["spec"]["metrics"][0]["external"]["metric"]["selector"]["matchLabels"]
    assert labels["infer_huawei_com_inferservice_name"] == "motor-0"


@pytest.mark.parametrize(
    "role, policy, error",
    [
        (make_engine_role("decode", replicas=0), {"decode": {}}, "zero-replica"),
        (make_engine_role("union"), {"union": {}}, "requires 'metric'"),
        (make_engine_role("prefill"), {"prefill": {"min_replicas": 4, "max_replicas": 2}}, "min_replicas"),
        (make_engine_role("prefill"), {"unknown": {}}, "enabled engine role"),
    ],
)
def test_hpa_rejects_invalid_target(role, policy, error):
    config = make_user_config(scaling_policy=policy)
    if role["name"] == "union":
        config["motor_deploy_config"]["hybrid_instances_num"] = 1
    with pytest.raises(ValueError, match=error):
        generate_scaling_resources(config, make_rbg(role))


def test_hpa_rejects_port_overflow_at_max_replicas():
    config = make_explicit_port_config(scaling_policy={"prefill": {"max_replicas": 2}})

    with pytest.raises(ValueError, match="exceeds 65535"):
        generate_scaling_resources(
            config,
            make_rbg(make_engine_role("prefill", base_port="64000", ports_enabled=True)),
        )


@pytest.mark.parametrize("enabled", [None, False])
def test_hpa_ignores_instance_port_offsets_when_feature_is_disabled(enabled):
    config = make_user_config(scaling_policy={"prefill": {"max_replicas": 2}})

    resources = generate_scaling_resources(
        config,
        make_rbg(make_engine_role("prefill", base_port="64000", ports_enabled=enabled)),
    )

    assert resources["hpas"]["prefill"]["spec"]["maxReplicas"] == 2


def test_hpa_accepts_last_instance_port_at_65535():
    config = make_explicit_port_config(scaling_policy={"prefill": {"max_replicas": 2}})

    resources = generate_scaling_resources(
        config,
        make_rbg(make_engine_role("prefill", base_port="61535", ports_enabled=True)),
    )

    assert resources["hpas"]["prefill"]["spec"]["maxReplicas"] == 2


def test_update_preserves_compatible_current_hpa():
    current = {"policy": None, "hpas": {"prefill": make_current_hpa()}}

    result = generate_scaling_resources(
        make_user_config(),
        make_rbg(make_engine_role("prefill")),
        is_update=True,
        current=current,
    )

    assert result == {"policy": None, "hpas": None}


@pytest.mark.parametrize(
    "current, error",
    [
        (None, "current is required"),
        ({"policy": None}, "missing required keys"),
        ({"policy": None, "hpas": {}, "facts": {}}, "unexpected keys"),
        ({"policy": "invalid", "hpas": {}}, "current.policy must be a dict or None"),
        ({"policy": None, "hpas": {"decode": make_current_hpa("decode")}}, "enabled engine role"),
    ],
)
def test_update_rejects_invalid_current_hpa_view(current, error):
    with pytest.raises(ValueError, match=error):
        generate_scaling_resources(
            make_user_config(),
            make_rbg(make_engine_role("prefill")),
            is_update=True,
            current=current,
        )


@pytest.mark.parametrize(
    "case, error",
    [
        ("namespace", "metadata.namespace"),
        ("managed-label", "managed-by"),
        ("target-kind", "scaleTargetRef.kind"),
        ("target-name", "scaleTargetRef.name"),
        ("metric-scope", "kubernetes_namespace"),
    ],
)
def test_update_rejects_incompatible_current_hpa(case, error):
    hpa = make_current_hpa()
    if case == "namespace":
        hpa["metadata"]["namespace"] = "other"
    elif case == "managed-label":
        hpa["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "external"
    elif case == "target-kind":
        hpa["spec"]["scaleTargetRef"]["kind"] = "Deployment"
    elif case == "target-name":
        hpa["spec"]["scaleTargetRef"]["name"] = "motor-decode"
    else:
        labels = hpa["spec"]["metrics"][0]["external"]["metric"]["selector"]["matchLabels"]
        labels["kubernetes_namespace"] = "other"
    current = {"policy": None, "hpas": {"prefill": hpa}}

    with pytest.raises(ValueError, match=error):
        generate_scaling_resources(
            make_user_config(),
            make_rbg(make_engine_role("prefill")),
            is_update=True,
            current=current,
        )


@pytest.mark.parametrize(
    "role, hpa, error",
    [
        (make_engine_role("prefill"), make_current_hpa(minimum=4, maximum=2), "minReplicas <= maxReplicas"),
    ],
)
def test_update_rejects_unsafe_preserved_hpa(role, hpa, error):
    current = {"policy": None, "hpas": {"prefill": hpa}}

    with pytest.raises(ValueError, match=error):
        generate_scaling_resources(make_user_config(), make_rbg(role), is_update=True, current=current)


def test_update_rejects_preserved_hpa_port_overflow_when_offsets_are_enabled():
    role = make_engine_role("prefill", base_port="64000", ports_enabled=True)
    current = {"policy": None, "hpas": {"prefill": make_current_hpa(maximum=2)}}

    with pytest.raises(ValueError, match="exceeds 65535"):
        generate_scaling_resources(
            make_explicit_port_config(),
            make_rbg(role),
            is_update=True,
            current=current,
        )


def test_explicit_hpa_disable_allows_removing_hpa_with_invalid_limits():
    current = {"policy": None, "hpas": {"prefill": make_current_hpa(minimum=4, maximum=2)}}

    result = generate_scaling_resources(
        make_user_config(scaling_policy={}),
        make_rbg(make_engine_role("prefill")),
        is_update=True,
        current=current,
    )

    assert result == {"policy": None, "hpas": {}}


def test_update_validates_preserved_policy_against_gang_setting():
    current = {
        "policy": {
            "spec": {
                "policies": [
                    {
                        "name": "engine-scaling",
                        "roles": ["prefill"],
                        "strategy": {"scaling": {"maxSkew": "50%", "progression": "OrderReady"}},
                    }
                ]
            }
        },
        "hpas": {},
    }

    with pytest.raises(ValueError, match="group gang"):
        generate_scaling_resources(
            make_user_config(), make_rbg(make_engine_role("prefill")), is_update=True, current=current
        )


def make_manual_config(p_instances=2, d_instances=1, **extra):
    result = {
        "motor_deploy_config": {
            "job_id": "motor-demo",
            "deploy_mode": "rbg",
            "p_instances_num": p_instances,
            "d_instances_num": d_instances,
            "rbg_config": {"name": "motor"},
        }
    }
    result.update(extra)
    return result


def make_online_rbg(*roles):
    result = make_rbg(*roles)
    result.update(apiVersion=C.RBG_API_VERSION, kind=C.RBG_KIND)
    result["metadata"].update(
        {
            "uid": "rbg-uid",
            "resourceVersion": "42",
            "labels": {
                "app.kubernetes.io/managed-by": "motor-deployer",
                "rbg.workloads.x-k8s.io/group-name": result["metadata"]["name"],
            },
        }
    )
    return result


def install_manual_cluster(
    monkeypatch,
    baseline,
    rbg,
    *,
    hpas=None,
    adapters=None,
    fail_action=None,
    patch_resource_version="43",
    configmap_data=None,
):
    calls = {"commands": [], "patches": [], "baseline_patches": [], "baselines": [], "configmaps": []}
    adapters = adapters or {}
    configmap_data = deepcopy(configmap_data or {"startup.sh": "preserved"})
    configmap_data["user_config.json"] = json.dumps(baseline, indent=2) + "\n"

    def run_command(arguments, timeout=60):
        arguments = [a for a in arguments if not a.startswith("--request-timeout=")]
        arguments = ["rolebasedgroup" if a == C.RBG_RESOURCE else a for a in arguments]
        calls["commands"].append((deepcopy(arguments), timeout))
        action, resource = arguments[1], arguments[2]
        if fail_action in (action, f"{action}_{resource}"):
            raise RuntimeError(f"simulated kubectl {action} failure")
        if action == "get" and resource == "rolebasedgroup":
            return json.dumps(rbg)
        if action == "get" and resource == "horizontalpodautoscalers":
            return json.dumps({"items": hpas or []})
        if action == "get" and resource == "rolebasedgroupscalingadapters":
            return json.dumps({"items": list(adapters.values())})
        if action == "get" and resource == "configmap":
            result = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "motor-config",
                    "namespace": arguments[arguments.index("-n") + 1],
                    "uid": "configmap-uid",
                    "resourceVersion": "7",
                    "labels": {
                        "existing": "label",
                        C.RBG_MANAGED_LABEL: C.RBG_MANAGED_VALUE,
                        C.RBG_GROUP_LABEL: rbg["metadata"]["name"],
                    },
                    "annotations": {"existing": "annotation", C.RBG_SERVICE_ID_ANNOTATION: "identity"},
                    "ownerReferences": [{"uid": "rbg-uid"}],
                },
                "data": configmap_data,
            }
            calls["configmaps"].append(deepcopy(result))
            return json.dumps(result)
        if action == "patch" and resource == "rolebasedgroup":
            operations = json.loads(Path(arguments[arguments.index("--patch-file") + 1]).read_text(encoding="utf-8"))
            calls["patches"].append(deepcopy(operations))
            result = deepcopy(rbg)
            for operation in operations:
                if operation["op"] != "replace":
                    continue
                index = int(operation["path"].split("/")[3])
                result["spec"]["roles"][index]["replicas"] = operation["value"]
            result["metadata"]["resourceVersion"] = patch_resource_version
            return json.dumps(result)
        if action == "patch" and resource == "configmap":
            operations = json.loads(Path(arguments[arguments.index("--patch-file") + 1]).read_text(encoding="utf-8"))
            calls["baseline_patches"].append(deepcopy(operations))
            serialized = operations[-1]["value"]
            calls["baselines"].append((arguments[arguments.index("-n") + 1], json.loads(serialized)))
            result_data = {**configmap_data, "user_config.json": serialized}
            result = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "motor-config",
                    "namespace": arguments[arguments.index("-n") + 1],
                    "uid": "configmap-uid",
                    "resourceVersion": "8",
                    "labels": {
                        "existing": "label",
                        C.RBG_MANAGED_LABEL: C.RBG_MANAGED_VALUE,
                        C.RBG_GROUP_LABEL: rbg["metadata"]["name"],
                    },
                    "annotations": {"existing": "annotation", C.RBG_SERVICE_ID_ANNOTATION: "identity"},
                    "ownerReferences": [{"uid": "rbg-uid"}],
                },
                "data": result_data,
            }
            calls["configmaps"].append(deepcopy(result))
            return json.dumps(result)
        raise AssertionError(f"unexpected kubectl command: {arguments}")

    monkeypatch.setattr(lifecycle, "run_cmd_get_output", run_command)
    return calls


def test_manual_scaling_patches_only_changed_role_replicas_and_updates_baseline(monkeypatch):
    baseline = make_manual_config()
    requested = make_manual_config(p_instances=3, d_instances=0)
    rbg = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode", replicas=1))
    preserved_data = {"startup.sh": "rendered startup", "env.json": "rendered env", "other": "preserved"}
    calls = install_manual_cluster(monkeypatch, baseline, rbg, configmap_data=preserved_data)

    update_rbg_instance_num(requested, baseline)

    assert calls["patches"] == [
        [
            {"op": "test", "path": "/metadata/uid", "value": "rbg-uid"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
            {"op": "test", "path": "/spec/roles/0/name", "value": "prefill"},
            {"op": "replace", "path": "/spec/roles/0/replicas", "value": 3},
            {"op": "test", "path": "/spec/roles/1/name", "value": "decode"},
            {"op": "replace", "path": "/spec/roles/1/replicas", "value": 0},
        ]
    ]
    patch_command, timeout = next(call for call in calls["commands"] if call[0][1] == "patch")
    assert patch_command[:7] == [
        "kubectl",
        "patch",
        "rolebasedgroup",
        "motor",
        "-n",
        "motor-demo",
        "--type=json",
    ]
    assert patch_command[-2:] == ["-o", "json"]
    assert timeout == 60
    saved = calls["baselines"][0]
    assert saved[0] == "motor-demo"
    assert saved[1]["motor_deploy_config"]["p_instances_num"] == 3
    assert saved[1]["motor_deploy_config"]["d_instances_num"] == 0
    assert calls["baseline_patches"] == [
        [
            {"op": "test", "path": "/metadata/uid", "value": "configmap-uid"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
            {
                "op": "replace",
                "path": "/data/user_config.json",
                "value": json.dumps(saved[1], ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            },
        ]
    ]
    configmap_command, configmap_timeout = next(
        call for call in calls["commands"] if call[0][1:3] == ["patch", "configmap"]
    )
    assert configmap_command[:7] == [
        "kubectl",
        "patch",
        "configmap",
        "motor-config",
        "-n",
        "motor-demo",
        "--type=json",
    ]
    assert configmap_command[-2:] == ["-o", "json"]
    assert configmap_timeout == 60
    assert len(calls["configmaps"]) == 2
    before, after = calls["configmaps"][0], calls["configmaps"][1]
    for key, value in preserved_data.items():
        assert before["data"][key] == value
        assert after["data"][key] == value
    for field in ("labels", "annotations", "ownerReferences"):
        assert after["metadata"][field] == before["metadata"][field]


def test_manual_scaling_does_not_repeat_instance_num_max_validation(monkeypatch):
    baseline = make_manual_config(p_instances=C.INSTANCE_NUM_MAX)
    requested = make_manual_config(p_instances=C.INSTANCE_NUM_MAX + 1)
    rbg = make_online_rbg(
        make_engine_role("prefill", replicas=C.INSTANCE_NUM_MAX),
        make_engine_role("decode"),
    )
    calls = install_manual_cluster(monkeypatch, baseline, rbg)

    update_rbg_instance_num(requested, baseline)

    assert any(
        operation.get("op") == "replace" and operation.get("value") == C.INSTANCE_NUM_MAX + 1
        for operation in calls["patches"][0]
    )


@pytest.mark.parametrize("automatic_owner", ["hpa", "adapter", "enabled"])
def test_manual_scaling_rejects_roles_still_under_automatic_control(monkeypatch, automatic_owner):
    baseline = make_manual_config()
    requested = make_manual_config(p_instances=3)
    role = make_engine_role("prefill", replicas=2)
    hpas = []
    adapters = {}
    if automatic_owner == "hpa":
        hpas = [
            {
                "spec": {
                    "scaleTargetRef": {
                        "apiVersion": "workloads.x-k8s.io/v1alpha2",
                        "kind": "RoleBasedGroupScalingAdapter",
                        "name": "motor-prefill",
                    }
                }
            }
        ]
    elif automatic_owner == "adapter":
        adapters["motor-prefill"] = {"metadata": {"name": "motor-prefill"}}
    else:
        role["scalingAdapter"] = {"enable": True}
    calls = install_manual_cluster(
        monkeypatch, baseline, make_online_rbg(role, make_engine_role("decode")), hpas=hpas, adapters=adapters
    )

    with pytest.raises(ValueError, match="cannot be scaled manually"):
        update_rbg_instance_num(requested, baseline)

    assert calls["patches"] == []
    assert calls["baselines"] == []


def test_manual_scaling_rejects_non_instance_configuration_changes(monkeypatch):
    baseline = make_manual_config(image_name="old-image")
    requested = make_manual_config(p_instances=3, image_name="new-image")
    rbg = make_online_rbg(make_engine_role("prefill"), make_engine_role("decode"))
    calls = install_manual_cluster(monkeypatch, baseline, rbg)

    with pytest.raises(ValueError, match="only allows instance number changes"):
        update_rbg_instance_num(requested, baseline)

    assert calls["patches"] == []


def test_manual_scaling_rejects_zero_encode_instances(monkeypatch):
    baseline = make_manual_config()
    baseline["motor_deploy_config"]["e_instances_num"] = 1
    requested = deepcopy(baseline)
    requested["motor_deploy_config"]["e_instances_num"] = 0
    rbg = make_online_rbg(
        make_engine_role("encode", replicas=1),
        make_engine_role("prefill"),
        make_engine_role("decode"),
    )
    install_manual_cluster(monkeypatch, baseline, rbg)

    with pytest.raises(ValueError, match="e_instances_num must be >= 1"):
        update_rbg_instance_num(requested, baseline)


def test_manual_scaling_recovers_baseline_when_online_target_was_already_applied(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    rbg = make_online_rbg(make_engine_role("prefill", replicas=3), make_engine_role("decode"))
    calls = install_manual_cluster(monkeypatch, baseline, rbg)

    update_rbg_instance_num(requested, baseline)

    assert calls["patches"] == []
    assert calls["baselines"][0][1]["motor_deploy_config"]["p_instances_num"] == 3


def test_manual_scaling_accepts_effective_p2d_default_in_baseline(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    baseline["motor_controller_config"] = {"fault_tolerance_config": {"enable_scale_p2d": False}}
    requested = make_manual_config(p_instances=3)
    rbg = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode"))
    calls = install_manual_cluster(monkeypatch, baseline, rbg)

    update_rbg_instance_num(requested, baseline)

    assert calls["patches"]
    assert calls["baselines"][0][1]["motor_controller_config"]["fault_tolerance_config"] == {"enable_scale_p2d": False}


def test_manual_scaling_ignores_port_offsets_when_feature_is_disabled(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    prefill = make_engine_role("prefill", replicas=2, base_port="64000", ports_enabled=False)
    rbg = make_online_rbg(prefill, make_engine_role("decode"))
    calls = install_manual_cluster(monkeypatch, baseline, rbg)

    update_rbg_instance_num(requested, baseline)

    assert calls["patches"]


def test_manual_scaling_uses_a_default_rbg_name_when_name_is_omitted(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    baseline["motor_deploy_config"]["rbg_config"] = {}
    requested["motor_deploy_config"]["rbg_config"] = {}
    rbg = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode"))
    rbg["metadata"]["name"] = "vllm"
    rbg["metadata"]["labels"]["rbg.workloads.x-k8s.io/group-name"] = "vllm"
    calls = install_manual_cluster(monkeypatch, baseline, rbg)

    update_rbg_instance_num(requested, baseline)

    assert calls["commands"][0] == (
        [
            "kubectl",
            "get",
            "rolebasedgroup",
            "vllm",
            "--ignore-not-found=true",
            "-n",
            "motor-demo",
            "-o",
            "json",
        ],
        60,
    )


def test_manual_scaling_ignores_same_name_deployment_hpa(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    rbg = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode"))
    hpas = [
        {
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "motor-prefill",
                }
            }
        }
    ]
    calls = install_manual_cluster(monkeypatch, baseline, rbg, hpas=hpas)

    update_rbg_instance_num(requested, baseline)

    assert calls["patches"]


def test_manual_scaling_does_not_save_baseline_when_patch_fails(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    rbg = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode"))
    calls = install_manual_cluster(monkeypatch, baseline, rbg, fail_action="patch")

    with pytest.raises(RuntimeError, match="simulated kubectl patch failure"):
        update_rbg_instance_num(requested, baseline)

    assert calls["baselines"] == []


def test_manual_scaling_rejects_invalid_kubectl_json(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    monkeypatch.setattr(lifecycle, "run_cmd_get_output", lambda *_args, **_kwargs: "not-json")

    with pytest.raises(json.JSONDecodeError):
        update_rbg_instance_num(requested, baseline)


def test_manual_scaling_does_not_save_baseline_for_invalid_patch_response(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    rbg = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode"))
    calls = install_manual_cluster(monkeypatch, baseline, rbg, patch_resource_version=None)

    with pytest.raises(RuntimeError, match="replaced"):
        update_rbg_instance_num(requested, baseline)

    assert calls["baselines"] == []


def test_manual_scaling_does_not_save_baseline_when_configmap_patch_fails(monkeypatch):
    baseline = make_manual_config(p_instances=2)
    requested = make_manual_config(p_instances=3)
    rbg = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode"))
    calls = install_manual_cluster(monkeypatch, baseline, rbg, fail_action="patch_configmap")

    with pytest.raises(RuntimeError, match="configuration baseline was not updated"):
        update_rbg_instance_num(requested, baseline)

    assert calls["patches"]
    assert calls["baselines"] == []


def test_disabling_role_removes_its_hpa_without_explicit_scaling_config():
    current = {"policy": None, "hpas": {"prefill": make_current_hpa()}}
    result = generate_scaling_resources(
        make_user_config(), make_rbg(make_engine_role("prefill", replicas=0)), is_update=True, current=current
    )
    assert result["hpas"] == {}


def test_pd_to_union_refreshes_default_policy():
    config = make_user_config()
    initial = generate_scaling_resources(config, make_rbg(make_engine_role("prefill"), make_engine_role("decode")))
    config["motor_deploy_config"]["hybrid_instances_num"] = 1
    group = make_rbg(
        make_engine_role("prefill", replicas=0),
        make_engine_role("decode", replicas=0),
        make_engine_role("union", replicas=1),
    )
    updated = generate_scaling_resources(config, group, is_update=True, current=initial)
    assert updated["policy"]["spec"]["policies"][0]["roles"] == ["union"]


def test_manual_scaling_accepts_unchanged_disabled_encode():
    from lib.rbg.scaling import _manual_targets

    baseline = make_manual_config()
    baseline["motor_deploy_config"]["e_instances_num"] = 0
    requested = deepcopy(baseline)
    requested["motor_deploy_config"]["p_instances_num"] = 3
    assert _manual_targets(requested, baseline, {"prefill": make_engine_role("prefill")}) == {"prefill": 3}


@pytest.mark.parametrize("window", ["before_read", "before_patch"])
def test_manual_scale_reports_config_conflict_and_retry_preserves_concurrent_change(monkeypatch, window):
    baseline = make_manual_config()
    requested = make_manual_config(p_instances=3)
    group = make_online_rbg(make_engine_role("prefill"), make_engine_role("decode"))
    latest = deepcopy(baseline)
    latest["motor_controller_config"] = {"logging_config": {"log_level": "DEBUG"}}
    runtime = {
        "metadata": {"uid": "cm", "resourceVersion": "7", "annotations": {C.RBG_SERVICE_ID_ANNOTATION: "id"}},
        "data": {"user_config.json": json.dumps(latest if window == "before_read" else baseline)},
    }
    monkeypatch.setattr(
        lifecycle, "_read_managed_resource", lambda resource, *a: group if resource == C.RBG_RESOURCE else runtime
    )
    monkeypatch.setattr(lifecycle, "_deletion_query", lambda *a: [])
    patches = []
    conflict = [window == "before_patch"]

    def patch(resource, live, operations):
        patches.append((resource, operations))
        assert operations[:2] == lifecycle._rbg_patch_tests(live)
        if resource == C.RBG_RESOURCE:
            group["spec"]["roles"][0]["replicas"] = 3
        elif conflict[0]:
            conflict[0] = False
            runtime["data"]["user_config.json"] = json.dumps(latest)
            runtime["metadata"]["resourceVersion"] = "8"
            raise RuntimeError("resourceVersion conflict")
        else:
            runtime["data"]["user_config.json"] = operations[-1]["value"]
        return live

    monkeypatch.setattr(lifecycle, "_patch_rbg_object", patch)
    with pytest.raises(RuntimeError, match="was updated.*baseline was not updated.*conflict"):
        update_rbg_instance_num(requested, baseline)
    assert json.loads(runtime["data"]["user_config.json"]) == latest
    retry = deepcopy(latest)
    retry["motor_deploy_config"]["p_instances_num"] = 3
    update_rbg_instance_num(retry, latest)
    assert json.loads(runtime["data"]["user_config.json"]) == retry
    assert sum(resource == C.RBG_RESOURCE for resource, _ in patches) == 1


def test_manual_scale_does_not_save_baseline_when_admission_changes_target_replicas(monkeypatch):
    baseline = make_manual_config()
    requested = make_manual_config(p_instances=3)
    group = make_online_rbg(make_engine_role("prefill", replicas=2), make_engine_role("decode"))
    install_manual_cluster(monkeypatch, baseline, group)
    writes = []

    def patch(resource, *args):
        writes.append(resource)
        return deepcopy(group)

    monkeypatch.setattr(lifecycle, "_patch_rbg_object", patch)
    with pytest.raises(RuntimeError, match="requested replicas"):
        update_rbg_instance_num(requested, baseline)
    assert writes == [C.RBG_RESOURCE]
