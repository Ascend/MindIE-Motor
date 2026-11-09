# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""docker_deploy.py --show-commands 对 KV 亲和的契约。"""

from pathlib import Path
import sys

import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

from lib.docker_commands import ConfigError, render  # noqa: E402


def _pd_config(*, conductor=False, affinity=False, store=False, hybrid=False, single=False):
    deploy = {
        "image_name": "img",
        "hardware_type": "800I_A3",
    }
    if single:
        deploy["deploy_mode"] = "single_container"
        deploy.update(
            {
                "p_instances_num": 1,
                "d_instances_num": 1,
                "single_p_instance_pod_num": 1,
                "single_d_instance_pod_num": 1,
            }
        )
    elif hybrid:
        deploy.update({"hybrid_instances_num": 2, "single_hybrid_instance_pod_num": 1})
    else:
        deploy.update(
            {
                "p_instances_num": 1,
                "d_instances_num": 1,
                "single_p_instance_pod_num": 1,
                "single_d_instance_pod_num": 1,
            }
        )
    cfg = {
        "motor_deploy_config": deploy,
        "motor_coordinator_config": {},
        "motor_engine_prefill_config": {"engine_config": {}},
        "motor_engine_union_config": {"engine_config": {}},
    }
    if affinity:
        cfg["motor_coordinator_config"]["scheduler_config"] = {"prefill_scheduler_type": "kv_cache_affinity"}
    if conductor:
        cfg["kv_conductor_config"] = {"http_server_port": 13333}
    if store:
        cfg["kv_cache_store_config"] = {"port": 50088}
    return cfg


def _text(cfg, *, in_container=False):
    return render(cfg, "/home/examples/config", "/home/examples/deployer/docker_deploy.py", in_container=in_container)


def test_show_commands_omits_kvc_when_disabled():
    text = _text(_pd_config())
    assert "motor-kvc" not in text
    assert "--kv-conductor-ip" not in text
    assert "--role kv_conductor" not in text


def test_show_commands_adds_shared_kvc_and_flags():
    text = _text(_pd_config(conductor=True, affinity=True))
    assert "服务器 1：kv_conductor" in text
    assert "--container-name motor-kvc" in text
    assert "--role kv_conductor" in text
    assert text.count("--kv-conductor-ip") >= 3
    assert "--kv-conductor-ip \"<服务器1 IP>\"" in text
    assert "--kv-conductor-ip \"<本机IP>\"" in text
    assert "motor-kvs" not in text


def test_show_commands_kvc_also_gets_kv_store_ip_when_pooling():
    text = _text(_pd_config(conductor=True, affinity=True, store=True))
    assert "服务器 1：kv_store" in text
    assert "服务器 1：kv_conductor" in text
    kvc_block = text.split("服务器 1：kv_conductor", 1)[1].split("服务器 1：", 1)[0]
    assert "--kv-store-ip" in kvc_block
    assert "--kv-conductor-ip" in text.split("服务器 1：kv_store", 1)[1]


def test_show_commands_hybrid_and_in_container():
    text = _text(_pd_config(conductor=True, affinity=True, hybrid=True), in_container=True)
    assert "--start" in text
    assert "--role union" in text
    assert "--kv-conductor-ip" in text
    assert "--container-name motor-kvc" in text


def test_show_commands_fail_closed_affinity_without_port():
    with pytest.raises(ConfigError, match="http_server_port"):
        _text(_pd_config(affinity=True))


def test_show_commands_fail_closed_single_container():
    with pytest.raises(ConfigError, match="single_container"):
        _text(_pd_config(conductor=True, affinity=True, single=True))
