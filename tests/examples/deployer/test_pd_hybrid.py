import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
import deploy as deploy_module  # noqa: E402
from lib.config_validator import (  # noqa: E402
    validate_deploy_mode_consistency,
    validate_only_instance_changed,
    validate_pd_hybrid_config,
)
from lib.generator import k8s_utils  # noqa: E402
from lib.generator.engine import generate_yaml_engine, validate_instance_nums  # noqa: E402
from lib.generator.infer_service import (  # noqa: E402
    generate_yaml_infer_service_set,
    update_infer_service_replicas_only,
    get_infer_role,
    _find_infer_service_set_doc,
)
from lib.utils import load_yaml  # noqa: E402
from lib.utils import set_env_to_shell  # noqa: E402


def make_pd_hybrid_user_config():
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "pd-hybrid",
            C.IMAGE_NAME: "mindie:latest",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.HYBRID_INSTANCES_NUM: 1,
            C.SINGLE_HYBRID_INSTANCE_POD_NUM: 1,
            C.HYBRID_POD_NPU_NUM: 4,
        },
        "motor_coordinator_config": {},
        C.MOTOR_ENGINE_UNION_CONFIG: {
            C.ENGINE_TYPE: C.ENGINE_TYPE_VLLM,
            "model_config": {
                "model_name": "qwen3-8B",
                "model_path": "/mnt/weight/qwen3_8B",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
            },
            C.ENGINE_CONFIG: {"max_model_len": 2048},
        },
    }


def make_pd_separation_user_config():
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "pd-separate",
            C.IMAGE_NAME: "mindie:latest",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.P_INSTANCES_NUM: 1,
            C.D_INSTANCES_NUM: 1,
            C.SINGER_P_INSTANCES_NUM: 1,
            C.SINGER_D_INSTANCES_NUM: 1,
            C.P_POD_NPU_NUM: 4,
            C.D_POD_NPU_NUM: 4,
        },
        C.MOTOR_ENGINE_PREFILL_CONFIG: {},
        "motor_engine_decode_config": {},
    }


def make_deploy_paths(tmp_path):
    return {
        "controller_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "controller_template.yaml"),
        "controller_output_yaml": str(tmp_path / "mindie_motor_controller.yaml"),
        "coordinator_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "coordinator_template.yaml"),
        "coordinator_output_yaml": str(tmp_path / "mindie_motor_coordinator.yaml"),
        "engine_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "engine_template.yaml"),
        "engine_output_yaml": str(tmp_path / k8s_utils.g_engine_base_name),
        "kv_pool_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "kv_pool_template.yaml"),
        "kv_pool_output_yaml": str(tmp_path / "mindie_motor_kv_pool.yaml"),
        "kv_conductor_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "kv_conductor_template.yaml"),
        "kv_conductor_output_yaml": str(tmp_path / "mindie_motor_kv_conductor.yaml"),
        "infer_service_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        "infer_service_output_yaml": str(tmp_path / "infer_service.yaml"),
        "single_container_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "single_container_template.yaml"),
        "single_container_output_yaml": str(tmp_path / "mindie_motor_single_container.yaml"),
        "mf_store_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "mf_store_template.yaml"),
        "mf_store_output_yaml": str(tmp_path / "mindie_motor_mf_store.yaml"),
    }


def test_validate_pd_hybrid_config_accepts_union_schema():
    user_config = make_pd_hybrid_user_config()

    validate_pd_hybrid_config(user_config)
    validate_instance_nums(user_config)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda cfg: cfg[C.MOTOR_DEPLOY_CONFIG].__setitem__(C.P_INSTANCES_NUM, 1),
        lambda cfg: cfg.__setitem__(C.MOTOR_ENGINE_PREFILL_CONFIG, {}),
        lambda cfg: cfg.__setitem__("motor_engine_decode_config", {}),
        lambda cfg: cfg[C.MOTOR_DEPLOY_CONFIG].__setitem__("engine_topology", "pd_hybrid"),
    ],
)
def test_validate_pd_hybrid_config_rejects_mixed_schema(mutate):
    user_config = make_pd_hybrid_user_config()
    mutate(user_config)

    with pytest.raises(ValueError):
        validate_pd_hybrid_config(user_config)


def test_generate_yaml_engine_creates_hybrid_workload(tmp_path):
    user_config = make_pd_hybrid_user_config()
    input_yaml = DEPLOYER_ROOT / "yaml_template" / "engine_template.yaml"
    output_base = tmp_path / "mindie_server"
    k8s_utils.g_generate_yaml_list = []

    generate_yaml_engine(str(input_yaml), str(output_base), user_config)

    assert len(k8s_utils.g_generate_yaml_list) == 1
    output_file = k8s_utils.g_generate_yaml_list[0]
    assert output_file.endswith("_u0.yaml")
    with open(output_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    container = data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    env = {item[C.NAME]: item[C.VALUE] for item in container[C.ENV] if C.VALUE in item}
    assert env[C.ENV_ROLE] == C.ROLE_UNION
    assert data[C.SPEC][C.REPLICAS] == 1
    assert container[C.RESOURCES][C.REQUESTS][C.ASCEND_910_NPU_NUM] == 4
    assert container[C.RESOURCES][C.LIMITS][C.ASCEND_910_NPU_NUM] == 4


def test_deploy_services_dry_run_uses_infer_service_set_for_hybrid(tmp_path, monkeypatch):
    user_config = make_pd_hybrid_user_config()
    monkeypatch.setattr(deploy_module, "get_deploy_paths", lambda: make_deploy_paths(tmp_path))
    k8s_utils.g_generate_yaml_list = []

    deploy_module.deploy_services(user_config, env_config_path=None, dry_run=True)

    assert any(path.endswith("infer_service.yaml") for path in k8s_utils.g_generate_yaml_list)
    assert not any(path.endswith("_u0.yaml") for path in k8s_utils.g_generate_yaml_list)


def test_deploy_services_dry_run_uses_multi_deployment_when_explicit(tmp_path, monkeypatch):
    user_config = make_pd_hybrid_user_config()
    user_config[C.MOTOR_DEPLOY_CONFIG][C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML
    monkeypatch.setattr(deploy_module, "get_deploy_paths", lambda: make_deploy_paths(tmp_path))
    k8s_utils.g_generate_yaml_list = []

    deploy_module.deploy_services(user_config, env_config_path=None, dry_run=True)

    assert any(path.endswith("_u0.yaml") for path in k8s_utils.g_generate_yaml_list)


def test_boot_script_routes_union_role_to_engine(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")

    startup_root = DEPLOYER_ROOT / "startup"
    boot_path = tmp_path / "boot.sh"
    common_path = tmp_path / "common.sh"
    engine_path = tmp_path / "engine.sh"
    patch_path = tmp_path / "patch_apply_shuffle_safetensors.py"

    boot_path.write_text((startup_root / "boot.sh").read_text(encoding="utf-8"), encoding="utf-8")
    common_path.write_text("set_common_env() { :; }\n", encoding="utf-8")
    engine_path.write_text('echo "engine-entry:$ROLE"\n', encoding="utf-8")
    patch_path.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")

    run_env = os.environ.copy()
    run_env["ROLE"] = "union"
    result = subprocess.run(
        [bash, str(boot_path)],
        env=run_env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "engine-entry:union" in result.stdout


def test_set_env_to_shell_generates_union_env_function_for_hybrid(tmp_path, monkeypatch):
    common_path = tmp_path / "common.sh"
    controller_path = tmp_path / "controller.sh"
    coordinator_path = tmp_path / "coordinator.sh"
    engine_path = tmp_path / "engine.sh"
    kv_pool_path = tmp_path / "kv_pool.sh"
    kv_conductor_path = tmp_path / "kv_conductor.sh"
    mf_store_path = tmp_path / "mf_store.sh"
    single_container_path = tmp_path / "single_container.sh"

    for path in [
        common_path,
        controller_path,
        coordinator_path,
        engine_path,
        kv_pool_path,
        kv_conductor_path,
        mf_store_path,
        single_container_path,
    ]:
        path.write_text("", encoding="utf-8")

    env_config_path = tmp_path / "env.json"
    env_config = {
        "motor_common_env": {"BASE_KEY": "base"},
        "motor_engine_union_env": {"UNION_ONLY_KEY": "union"},
        "motor_engine_prefill_env": {"PREFILL_ONLY_KEY": "prefill"},
        "motor_engine_decode_env": {"DECODE_ONLY_KEY": "decode"},
        "motor_controller_env": {},
        "motor_coordinator_env": {},
        "motor_kv_cache_pool_env": {},
        "motor_kv_conductor_env": {},
        "motor_mf_store_env": {},
    }
    env_config_path.write_text(json.dumps(env_config), encoding="utf-8")

    monkeypatch.setattr(C, "COMMON_SHELL_PATH", str(common_path))
    monkeypatch.setattr(C, "CONTROLLER_SHELL_PATH", str(controller_path))
    monkeypatch.setattr(C, "COORDINATOR_SHELL_PATH", str(coordinator_path))
    monkeypatch.setattr(C, "ENGINE_SHELL_PATH", str(engine_path))
    monkeypatch.setattr(C, "KV_POOL_SHELL_PATH", str(kv_pool_path))
    monkeypatch.setattr(C, "KV_CONDUCTOR_SHELL_PATH", str(kv_conductor_path))
    monkeypatch.setattr(C, "MF_STORE_SHELL_PATH", str(mf_store_path))
    monkeypatch.setattr(C, "SINGLE_CONTAINER_SHELL_PATH", str(single_container_path))

    set_env_to_shell(
        make_pd_hybrid_user_config(),
        str(env_config_path),
        C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML,
    )

    engine_shell = engine_path.read_text(encoding="utf-8")
    assert "function set_union_env()" in engine_shell
    assert 'export UNION_ONLY_KEY="union"' in engine_shell


def test_validate_only_instance_changed_allows_hybrid_instance_count_change():
    baseline_config = make_pd_hybrid_user_config()
    current_config = make_pd_hybrid_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG][C.HYBRID_INSTANCES_NUM] = 2

    validate_only_instance_changed(current_config, baseline_config)


def test_validate_only_instance_changed_ignores_deploy_mode_when_current_omits_it():
    """ConfigMap baseline may inject deploy_mode while local user_config omits it."""
    baseline_config = make_pd_hybrid_user_config()
    baseline_config[C.MOTOR_DEPLOY_CONFIG][C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_INFER_SERVICE_SET
    current_config = make_pd_hybrid_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG].pop(C.DEPLOY_MODE_CONFIG_KEY, None)
    current_config[C.MOTOR_DEPLOY_CONFIG][C.HYBRID_INSTANCES_NUM] = 2

    validate_only_instance_changed(current_config, baseline_config)


def test_validate_only_instance_changed_allows_pd_separation_when_current_omits_deploy_mode():
    baseline_config = make_pd_separation_user_config()
    baseline_config[C.MOTOR_DEPLOY_CONFIG][C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_INFER_SERVICE_SET
    current_config = make_pd_separation_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG].pop(C.DEPLOY_MODE_CONFIG_KEY, None)
    current_config[C.MOTOR_DEPLOY_CONFIG][C.P_INSTANCES_NUM] = 2

    validate_only_instance_changed(current_config, baseline_config)


def test_validate_deploy_mode_consistency_allows_omitted_when_baseline_has_infer_service_set():
    baseline_deploy = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    baseline_deploy[C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_INFER_SERVICE_SET
    current_deploy = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    current_deploy.pop(C.DEPLOY_MODE_CONFIG_KEY, None)

    validate_deploy_mode_consistency(current_deploy, baseline_deploy)


def test_validate_deploy_mode_consistency_rejects_explicit_mode_change():
    baseline_deploy = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    baseline_deploy[C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_INFER_SERVICE_SET
    current_deploy = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    current_deploy[C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML

    with pytest.raises(ValueError, match=C.DEPLOY_MODE_CONFIG_KEY):
        validate_deploy_mode_consistency(current_deploy, baseline_deploy)


def test_validate_only_instance_changed_rejects_hybrid_non_instance_change():
    baseline_config = make_pd_hybrid_user_config()
    current_config = make_pd_hybrid_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG][C.HYBRID_POD_NPU_NUM] = 8

    with pytest.raises(ValueError):
        validate_only_instance_changed(current_config, baseline_config)


def test_validate_only_instance_changed_allows_pd_separation_instance_count_change():
    baseline_config = make_pd_separation_user_config()
    current_config = make_pd_separation_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG][C.P_INSTANCES_NUM] = 2

    validate_only_instance_changed(current_config, baseline_config)


def test_validate_only_instance_changed_rejects_pd_separation_non_instance_change():
    baseline_config = make_pd_separation_user_config()
    current_config = make_pd_separation_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG][C.P_POD_NPU_NUM] = 8

    with pytest.raises(ValueError):
        validate_only_instance_changed(current_config, baseline_config)


def test_handle_update_config_rejects_hybrid_instance_count_change(monkeypatch):
    baseline_config = make_pd_hybrid_user_config()
    current_config = make_pd_hybrid_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG][C.HYBRID_INSTANCES_NUM] = 2
    monkeypatch.setattr(deploy_module, "get_baseline_config_from_configmap", lambda _: baseline_config)

    with pytest.raises(ValueError, match=C.HYBRID_INSTANCES_NUM):
        deploy_module.handle_update_config(current_config)


def test_handle_update_instance_num_scales_hybrid_instances(tmp_path, monkeypatch):
    baseline_config = make_pd_hybrid_user_config()
    current_config = make_pd_hybrid_user_config()
    baseline_config[C.MOTOR_DEPLOY_CONFIG][C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML
    current_config[C.MOTOR_DEPLOY_CONFIG][C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML
    current_config[C.MOTOR_DEPLOY_CONFIG][C.HYBRID_INSTANCES_NUM] = 3
    commands = []
    monkeypatch.setattr(deploy_module, "get_baseline_config_from_configmap", lambda _: baseline_config)
    monkeypatch.setattr(deploy_module, "get_deploy_paths", lambda: make_deploy_paths(tmp_path))
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(tmp_path))
    monkeypatch.setattr(k8s_utils, "create_motor_config_configmap", lambda *_a, **_k: None)
    monkeypatch.setattr(k8s_utils, "safe_exec_cmd", commands.append)

    deploy_module.handle_update_instance_num(current_config)

    assert any(path.endswith("_u2.yaml") for path in k8s_utils.g_generate_yaml_list)
    assert commands == [
        f"kubectl apply -f {tmp_path / 'vllm_u1.yaml'} -n pd-hybrid",
        f"kubectl apply -f {tmp_path / 'vllm_u2.yaml'} -n pd-hybrid",
    ]


def test_elastic_distributed_engine_deploy_scales_out_hybrid_instances(tmp_path, monkeypatch):
    deploy_config = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    baseline_deploy_config = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    deploy_config[C.HYBRID_INSTANCES_NUM] = 3
    baseline_deploy_config[C.HYBRID_INSTANCES_NUM] = 1
    commands = []
    monkeypatch.setattr(k8s_utils, "safe_exec_cmd", commands.append)
    monkeypatch.setattr(k8s_utils, "g_engine_base_name", "mindie_server")

    k8s_utils.elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, str(tmp_path))

    assert commands == [
        f"kubectl apply -f {tmp_path / 'mindie_server_u1.yaml'} -n pd-hybrid",
        f"kubectl apply -f {tmp_path / 'mindie_server_u2.yaml'} -n pd-hybrid",
    ]


def test_elastic_distributed_engine_deploy_scales_in_hybrid_instances(tmp_path, monkeypatch):
    deploy_config = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    baseline_deploy_config = make_pd_hybrid_user_config()[C.MOTOR_DEPLOY_CONFIG]
    deploy_config[C.HYBRID_INSTANCES_NUM] = 1
    baseline_deploy_config[C.HYBRID_INSTANCES_NUM] = 3
    yaml_to_remove = tmp_path / "mindie_server_u2.yaml"
    yaml_to_remove.write_text("kind: Deployment\n", encoding="utf-8")
    commands = []
    monkeypatch.setattr(k8s_utils, "safe_exec_cmd", commands.append)
    monkeypatch.setattr(k8s_utils, "g_engine_base_name", "mindie_server")

    k8s_utils.elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, str(tmp_path))

    assert commands == [
        f"kubectl delete -f {tmp_path / 'mindie_server_u2.yaml'} -n pd-hybrid",
        f"kubectl delete -f {tmp_path / 'mindie_server_u1.yaml'} -n pd-hybrid",
    ]
    assert not yaml_to_remove.exists()


def test_generate_yaml_infer_service_set_configures_union_for_hybrid(tmp_path, monkeypatch):
    user_config = make_pd_hybrid_user_config()
    paths = make_deploy_paths(tmp_path)
    k8s_utils.g_generate_yaml_list = []
    monkeypatch.setattr(k8s_utils, "g_controller_service", "ctrl.pd-hybrid.svc.cluster.local")
    monkeypatch.setattr(k8s_utils, "g_coordinator_service", "coord.pd-hybrid.svc.cluster.local")

    generate_yaml_infer_service_set(
        paths["infer_service_input_yaml"],
        paths["infer_service_output_yaml"],
        user_config,
    )

    all_docs = load_yaml(paths["infer_service_output_yaml"], False)
    infer_doc = _find_infer_service_set_doc(all_docs)
    union_role = get_infer_role(infer_doc, C.ROLE_UNION)
    prefill_role = get_infer_role(infer_doc, C.ROLE_PREFILL)
    decode_role = get_infer_role(infer_doc, C.ROLE_DECODE)

    assert union_role[C.REPLICAS] == 1
    assert prefill_role[C.REPLICAS] == 0
    assert decode_role[C.REPLICAS] == 0
    container = union_role[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    env = {item[C.NAME]: item[C.VALUE] for item in container[C.ENV] if C.VALUE in item}
    assert env[C.ENV_ROLE] == C.ROLE_UNION


def test_generate_yaml_infer_service_set_zeros_union_for_pd_separation(tmp_path, monkeypatch):
    user_config = make_pd_separation_user_config()
    paths = make_deploy_paths(tmp_path)
    k8s_utils.g_generate_yaml_list = []
    monkeypatch.setattr(k8s_utils, "g_controller_service", "ctrl.pd-separate.svc.cluster.local")
    monkeypatch.setattr(k8s_utils, "g_coordinator_service", "coord.pd-separate.svc.cluster.local")

    generate_yaml_infer_service_set(
        paths["infer_service_input_yaml"],
        paths["infer_service_output_yaml"],
        user_config,
    )

    all_docs = load_yaml(paths["infer_service_output_yaml"], False)
    infer_doc = _find_infer_service_set_doc(all_docs)
    union_role = get_infer_role(infer_doc, C.ROLE_UNION)
    prefill_role = get_infer_role(infer_doc, C.ROLE_PREFILL)
    decode_role = get_infer_role(infer_doc, C.ROLE_DECODE)

    assert union_role[C.REPLICAS] == 0
    assert prefill_role[C.REPLICAS] == 1
    assert decode_role[C.REPLICAS] == 1


def test_update_infer_service_replicas_only_updates_union_for_hybrid(tmp_path):
    user_config = make_pd_hybrid_user_config()
    paths = make_deploy_paths(tmp_path)
    k8s_utils.g_generate_yaml_list = []
    generate_yaml_infer_service_set(
        paths["infer_service_input_yaml"],
        paths["infer_service_output_yaml"],
        user_config,
    )

    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    deploy_config[C.HYBRID_INSTANCES_NUM] = 3
    update_infer_service_replicas_only(paths["infer_service_output_yaml"], deploy_config)

    all_docs = load_yaml(paths["infer_service_output_yaml"], False)
    infer_doc = _find_infer_service_set_doc(all_docs)
    assert get_infer_role(infer_doc, C.ROLE_UNION)[C.REPLICAS] == 3


def test_handle_update_instance_num_scales_hybrid_via_crd_when_current_omits_deploy_mode(tmp_path, monkeypatch):
    """Scale succeeds when baseline has injected deploy_mode but local user_config omits it."""
    baseline_config = make_pd_hybrid_user_config()
    baseline_config[C.MOTOR_DEPLOY_CONFIG][C.DEPLOY_MODE_CONFIG_KEY] = C.DEPLOY_MODE_INFER_SERVICE_SET
    current_config = make_pd_hybrid_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG].pop(C.DEPLOY_MODE_CONFIG_KEY, None)
    current_config[C.MOTOR_DEPLOY_CONFIG][C.HYBRID_INSTANCES_NUM] = 2
    commands = []
    paths = make_deploy_paths(tmp_path)
    monkeypatch.setattr(deploy_module, "get_baseline_config_from_configmap", lambda _: baseline_config)
    monkeypatch.setattr(deploy_module, "get_deploy_paths", lambda: paths)
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(tmp_path))
    monkeypatch.setattr(k8s_utils, "create_motor_config_configmap", lambda *_a, **_k: None)
    monkeypatch.setattr(k8s_utils, "safe_exec_cmd", commands.append)
    monkeypatch.setattr(k8s_utils, "g_controller_service", "ctrl.pd-hybrid.svc.cluster.local")
    monkeypatch.setattr(k8s_utils, "g_coordinator_service", "coord.pd-hybrid.svc.cluster.local")

    generate_yaml_infer_service_set(
        paths["infer_service_input_yaml"],
        paths["infer_service_output_yaml"],
        baseline_config,
    )

    deploy_module.handle_update_instance_num(current_config)

    assert commands == [f"kubectl apply -f {paths['infer_service_output_yaml']} -n pd-hybrid"]
    all_docs = load_yaml(paths["infer_service_output_yaml"], False)
    infer_doc = _find_infer_service_set_doc(all_docs)
    assert get_infer_role(infer_doc, C.ROLE_UNION)[C.REPLICAS] == 2


def test_handle_update_instance_num_scales_hybrid_via_crd(tmp_path, monkeypatch):
    baseline_config = make_pd_hybrid_user_config()
    current_config = make_pd_hybrid_user_config()
    current_config[C.MOTOR_DEPLOY_CONFIG][C.HYBRID_INSTANCES_NUM] = 2
    commands = []
    paths = make_deploy_paths(tmp_path)
    monkeypatch.setattr(deploy_module, "get_baseline_config_from_configmap", lambda _: baseline_config)
    monkeypatch.setattr(deploy_module, "get_deploy_paths", lambda: paths)
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(tmp_path))
    monkeypatch.setattr(k8s_utils, "create_motor_config_configmap", lambda *_a, **_k: None)
    monkeypatch.setattr(k8s_utils, "safe_exec_cmd", commands.append)
    monkeypatch.setattr(k8s_utils, "g_controller_service", "ctrl.pd-hybrid.svc.cluster.local")
    monkeypatch.setattr(k8s_utils, "g_coordinator_service", "coord.pd-hybrid.svc.cluster.local")

    generate_yaml_infer_service_set(
        paths["infer_service_input_yaml"],
        paths["infer_service_output_yaml"],
        baseline_config,
    )

    deploy_module.handle_update_instance_num(current_config)

    assert commands == [f"kubectl apply -f {paths['infer_service_output_yaml']} -n pd-hybrid"]
    all_docs = load_yaml(paths["infer_service_output_yaml"], False)
    infer_doc = _find_infer_service_set_doc(all_docs)
    assert get_infer_role(infer_doc, C.ROLE_UNION)[C.REPLICAS] == 2


def test_vllm_pd_hybrid_sample_is_valid():
    sample_path = DEPLOYER_ROOT.parent / "infer_engines" / "vllm" / "pd_hybrid" / "user_config.json"
    with open(sample_path, "r", encoding="utf-8") as f:
        user_config = json.load(f)

    validate_pd_hybrid_config(user_config)
    validate_instance_nums(user_config)
    assert user_config[C.MOTOR_DEPLOY_CONFIG][C.DEPLOY_MODE_CONFIG_KEY] == C.DEPLOY_MODE_INFER_SERVICE_SET
