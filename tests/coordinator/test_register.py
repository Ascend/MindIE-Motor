# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import io
import json
import os
import tempfile
import urllib.error
from argparse import Namespace
from unittest.mock import patch

import pytest

from motor.coordinator import register


def _args(**kwargs) -> Namespace:
    values = {
        "action": "set",
        "prefill": ["10.0.0.1:8000"],
        "decode": ["10.0.0.2:8000"],
        "ids": [],
        "model_name": None,
        "engine_type": "vllm",
        "no_health_check": True,
    }
    values.update(kwargs)
    return Namespace(**values)


def _models_body(*ids: str) -> str:
    return json.dumps({"data": [{"id": model_id} for model_id in ids]})


def test_parse_args_defaults_to_set():
    args = register.parse_args(["--prefill", "10.0.0.1:8000"])
    assert args.action == "set"
    assert args.prefill == ["10.0.0.1:8000"]


def test_parse_args_del_requires_target():
    with pytest.raises(SystemExit):
        register.parse_args(["del"])


def test_parse_args_list():
    args = register.parse_args(["list"])
    assert args.action == "list"


def test_parse_args_list_rejects_endpoints():
    with pytest.raises(SystemExit):
        register.parse_args(["list", "--prefill", "10.0.0.1:8000"])


def test_parse_args_id_only_for_del():
    with pytest.raises(SystemExit):
        register.parse_args(["--prefill", "10.0.0.1:8000", "--id", "1"])


def test_derive_instance_id_is_stable_and_role_scoped():
    group = "10.0.0.1:8000,10.0.0.1:8001"
    first = register.derive_instance_id("prefill", group)
    assert first == register.derive_instance_id("prefill", group)
    assert first == register.derive_instance_id("prefill", "10.0.0.1:8001,10.0.0.1:8000")
    assert first != register.derive_instance_id("prefill", "10.0.0.1:8000")
    assert first != register.derive_instance_id("decode", group)
    assert first != register.derive_instance_id("prefill", "10.0.0.9:8000")
    assert 0x40000000 <= first <= 0x7FFFFFFF


def test_build_instances_canonicalizes_reordered_endpoint_group():
    first = register.build_instances(
        _args(prefill=["10.0.0.2:8001,10.0.0.1:8000"], decode=[]),
        "Qwen3-8B",
    )[0]
    reordered = register.build_instances(
        _args(prefill=["10.0.0.1:8000,10.0.0.2:8001"], decode=[]),
        "Qwen3-8B",
    )[0]

    assert first == reordered
    assert first["job_name"] == "Qwen3-8B-prefill-10.0.0.1-8000"
    assert first["endpoints"]["10.0.0.1"][0]["id"] == 0
    assert first["endpoints"]["10.0.0.2"][1]["id"] == 1


def test_build_instances_rejects_crc32_collision_within_request():
    args = _args(
        prefill=["10.2.126.188:8516", "10.3.57.36:8236"],
        decode=[],
    )

    with pytest.raises(SystemExit, match="duplicate instance ID"):
        register.build_instances(args, "Qwen3-8B")


def test_resolve_model_name_from_first_engine():
    with patch.object(
        register,
        "http_json",
        return_value=(200, _models_body("Qwen3-8B")),
    ) as mocked:
        name = register.resolve_model_name(_args())
    assert name == "Qwen3-8B"
    mocked.assert_called_once()
    assert mocked.call_args.args[1] == "http://10.0.0.1:8000/v1/models"


def test_resolve_model_name_skips_unreachable_engine():
    def fake_http(_method, url, **_kwargs):
        if "10.0.0.1" in url:
            raise OSError("down")
        return 200, _models_body("from-decode")

    with patch.object(register, "http_json", side_effect=fake_http):
        assert register.resolve_model_name(_args()) == "from-decode"


def test_build_instances_del_by_endpoint_skips_probe():
    args = _args(action="del", decode=[], ids=[], no_health_check=False)
    instances = register.build_instances(args, None)
    assert len(instances) == 1
    assert instances[0]["id"] == register.derive_instance_id("prefill", "10.0.0.1:8000")
    assert instances[0]["role"] == "prefill"


def test_build_instances_keeps_all_dp_endpoints_when_probe_passes():
    args = _args(prefill=["10.0.0.1:8000,10.0.0.1:8001"], decode=[], no_health_check=False)
    with patch.object(register, "probe_endpoint", return_value=True):
        instances = register.build_instances(args, "Qwen3-8B")
    assert len(instances) == 1
    assert list(instances[0]["endpoints"]["10.0.0.1"]) == [0, 1]


def test_build_instances_refuses_partial_dp_group_when_probe_fails():
    args = _args(prefill=["10.0.0.1:8000,10.0.0.1:8001"], decode=[], no_health_check=False)

    def fake_probe(_ip, port, _model_name):
        return port != "8001"

    with patch.object(register, "probe_endpoint", side_effect=fake_probe):
        with pytest.raises(SystemExit, match="10.0.0.1:8001"):
            register.build_instances(args, "Qwen3-8B")


def test_probe_endpoint_rejects_empty_model_list(capsys):
    with patch.object(register, "http_json", return_value=(200, _models_body())):
        assert register.probe_endpoint("10.0.0.1", "8000", "Qwen3-8B") is False
    assert "returned no models" in capsys.readouterr().err


def test_build_instances_explicit_model_rejects_empty_model_list():
    args = _args(prefill=["10.0.0.1:8000"], decode=[], no_health_check=False)
    with patch.object(register, "http_json", return_value=(200, _models_body())):
        with pytest.raises(SystemExit, match="probe failed"):
            register.build_instances(args, "Qwen3-8B")


def test_build_instances_no_health_check_skips_empty_model_list():
    args = _args(prefill=["10.0.0.1:8000"], decode=[], no_health_check=True)
    with patch.object(register, "http_json") as mocked:
        instances = register.build_instances(args, "Qwen3-8B")
    assert len(instances) == 1
    mocked.assert_not_called()


def test_main_dry_run_add_event(capsys):
    with patch.object(register, "http_json", return_value=(200, _models_body("Qwen3-8B"))):
        register.main(["add", "--prefill", "10.0.0.1:8000", "--dry-run", "--no-health-check"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "add"
    assert payload["instances"][0]["id"] == register.derive_instance_id("prefill", "10.0.0.1:8000")


def test_main_dry_run_del_by_id(capsys):
    register.main(["del", "--id", "42", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "del"
    assert payload["instances"][0]["id"] == 42


def test_main_del_by_id_submits_registered_identity(capsys):
    registered = {
        "count": 1,
        "instances": [
            {
                "id": 42,
                "role": "decode",
                "job_name": "Qwen3-8B-decode-10.0.0.2-8000",
                "model_name": "Qwen3-8B",
                "engine_type": "vllm",
                "endpoints": [],
            }
        ],
    }

    def fake_http(method, url, body=None, timeout=10):
        if url.endswith("/liveness"):
            return 200, '{"status":"ok"}'
        if url.endswith("/instances"):
            return 200, json.dumps(registered)
        if url.endswith("/instances/refresh"):
            assert method == "POST"
            instance = body["instances"][0]
            assert instance["role"] == "decode"
            assert instance["job_name"] == "Qwen3-8B-decode-10.0.0.2-8000"
            assert instance["model_name"] == "Qwen3-8B"
            return 200, '{"status":"success"}'
        if url.endswith("/readiness"):
            return 200, '{"ready":true}'
        raise AssertionError(f"unexpected {method} {url}")

    with patch.object(register, "http_json", side_effect=fake_http):
        register.main(["del", "--id", "42"])

    assert "Qwen3-8B-decode-10.0.0.2-8000(decode/42)" in capsys.readouterr().out


def test_main_list(capsys):
    listed = {"count": 1, "instances": [{"id": 1, "role": "prefill"}]}

    def fake_http(method, url, body=None, timeout=10):
        if url.endswith("/liveness"):
            return 200, '{"status":"ok"}'
        if url.endswith("/instances"):
            assert method == "GET"
            assert body is None
            return 200, json.dumps(listed)
        raise AssertionError(f"unexpected {method} {url}")

    with patch.object(register, "http_json", side_effect=fake_http):
        register.main(["list"])
    assert json.loads(capsys.readouterr().out) == listed


def test_main_list_sends_management_api_key_from_file(capsys):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as key_file:
        key_file.write("register-management-key\n")
        api_key_file = key_file.name
    listed = {"count": 0, "instances": []}

    def fake_http(method, url, body=None, timeout=10, headers=None):
        if url.endswith("/liveness"):
            assert headers is None
            return 200, '{"status":"ok"}'
        if url.endswith("/instances"):
            assert method == "GET"
            assert body is None
            assert headers == {"X-Motor-Management-Key": "register-management-key"}
            return 200, json.dumps(listed)
        raise AssertionError(f"unexpected {method} {url}")

    try:
        with patch.object(register, "http_json", side_effect=fake_http):
            register.main(["list", "--mgmt-api-key-file", api_key_file])
        assert json.loads(capsys.readouterr().out) == listed
    finally:
        os.remove(api_key_file)


def test_main_refresh_http_error_exits_with_status():
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:1026/instances/refresh",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=io.BytesIO(b'{"detail":"invalid instance"}'),
    )

    def fake_http(method, url, body=None, timeout=10):
        if url.endswith("/liveness"):
            return 200, '{"status":"ok"}'
        if url.endswith("/instances"):
            return 200, '{"count":0,"instances":[]}'
        if url.endswith("/instances/refresh"):
            raise err
        raise AssertionError(f"unexpected {method} {url}")

    with patch.object(register, "http_json", side_effect=fake_http):
        with pytest.raises(SystemExit, match=r"HTTP 400.*invalid instance"):
            register.main(["--prefill", "10.0.0.1:8000", "--no-health-check", "--model-name", "Qwen3-8B"])


def test_prepare_endpoint_delete_uses_registered_instance_id():
    instances = register.build_instances(
        _args(action="del", prefill=["10.0.0.1:8000"], decode=[]),
        None,
    )
    registered = [
        {
            "id": 77,
            "role": "prefill",
            "job_name": "Qwen3-8B-prefill-10.0.0.1-8000",
            "model_name": "Qwen3-8B",
            "endpoints": [
                {"id": 0, "ip": "10.0.0.1", "business_port": "8000", "headless": False},
            ],
        }
    ]

    register._prepare_instances_for_refresh("del", instances, registered)

    assert instances[0]["id"] == 77
    assert instances[0]["job_name"] == "Qwen3-8B-prefill-10.0.0.1-8000"
    assert instances[0]["model_name"] == "Qwen3-8B"


def test_prepare_endpoint_delete_matches_reordered_group():
    instances = register.build_instances(
        _args(action="del", prefill=["10.0.0.1:8001,10.0.0.1:8000"], decode=[]),
        None,
    )
    registered = [
        {
            "id": 77,
            "role": "prefill",
            "job_name": "Qwen3-8B-prefill-10.0.0.1-8000",
            "model_name": "Qwen3-8B",
            "endpoints": [
                {"id": 0, "ip": "10.0.0.1", "business_port": "8000", "headless": False},
                {"id": 1, "ip": "10.0.0.1", "business_port": "8001", "headless": False},
            ],
        }
    ]

    register._prepare_instances_for_refresh("del", instances, registered)

    assert instances[0]["id"] == 77


def test_prepare_id_delete_uses_registered_identity():
    instances = register.build_instances(_args(action="del", prefill=[], decode=[], ids=[77]), None)
    registered = [
        {
            "id": 77,
            "role": "decode",
            "job_name": "Qwen3-8B-decode-10.0.0.2-8000",
            "model_name": "Qwen3-8B",
            "engine_type": "vllm",
            "endpoints": [],
        }
    ]

    register._prepare_instances_for_refresh("del", instances, registered)

    assert instances[0]["role"] == "decode"
    assert instances[0]["job_name"] == "Qwen3-8B-decode-10.0.0.2-8000"
    assert instances[0]["model_name"] == "Qwen3-8B"
    assert instances[0]["engine_type"] == "vllm"


def test_prepare_id_delete_rejects_unknown_id():
    instances = register.build_instances(_args(action="del", prefill=[], decode=[], ids=[77]), None)

    with pytest.raises(SystemExit, match="no registered instance with ID 77"):
        register._prepare_instances_for_refresh("del", instances, [])
