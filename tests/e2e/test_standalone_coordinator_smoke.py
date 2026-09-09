# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Opt-in smoke for standalone Coordinator membership events and restart replay.

Required environment:
* MOTOR_RUN_STANDALONE_COORDINATOR_SMOKE=1
* MOTOR_STANDALONE_PREFILL_URL=http://host:port
* MOTOR_STANDALONE_DECODE_URL=http://host:port
* MOTOR_STANDALONE_MODEL=<served model>
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest


pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _required_url(name: str) -> tuple[str, int]:
    value = os.getenv(name, "")
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname or parsed.port is None:
        pytest.fail(f"{name} must be an http://host:port URL")
    return parsed.hostname, parsed.port


def _json_request(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            # 服务端返回 JSON 错误体时，本场景保留结构化诊断信息。
            return error.code, json.loads(error.read())
        except (json.JSONDecodeError, ValueError):
            # 网关等组件返回纯文本错误体时，本场景降级为空字典，避免掩盖真实 HTTP 状态码。
            return error.code, {}


def _wait_for_readiness(url: str, expected: bool, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, body = _json_request(url)
            if status == 200 and body.get("ready") is expected:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    pytest.fail(f"readiness did not become {expected}")


def _start_coordinator(config_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["USER_CONFIG_PATH"] = str(config_path)
    log_path = config_path.with_name("coordinator-smoke.log")
    # 日志持续写入文件，本场景不会因无人消费 stdout PIPE 而填满管道并卡住子进程。
    with log_path.open("a", encoding="utf-8") as log_stream:
        return subprocess.Popen(
            [sys.executable, "-m", "motor.coordinator.main"],
            env=env,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
        )


def _stop_coordinator(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(
    os.getenv("MOTOR_RUN_STANDALONE_COORDINATOR_SMOKE") != "1",
    reason="set MOTOR_RUN_STANDALONE_COORDINATOR_SMOKE=1 to run",
)
def test_standalone_membership_events_inference_and_restart_replay(tmp_path):
    prefill_host, prefill_port = _required_url("MOTOR_STANDALONE_PREFILL_URL")
    decode_host, decode_port = _required_url("MOTOR_STANDALONE_DECODE_URL")
    model = os.getenv("MOTOR_STANDALONE_MODEL", "").strip()
    if not model:
        pytest.fail("MOTOR_STANDALONE_MODEL is required")

    infer_port, mgmt_port, obs_port = _free_port(), _free_port(), _free_port()
    config = {
        "motor_coordinator_config": {
            "aigw": {
                "id": model,
                "p_max_seqlen": 32768,
                "d_max_seqlen": 32768,
            },
            "api_config": {
                "coordinator_api_host": "127.0.0.1",
                "coordinator_api_infer_port": infer_port,
                "coordinator_api_mgmt_port": mgmt_port,
                "coordinator_obs_port": obs_port,
            },
            "inference_workers_config": {
                "num_workers": 1,
                "worker_metaserver_base_port": _free_port(),
            },
        }
    }
    config_path = tmp_path / "coordinator.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    topology = {
        "event": "set",
        "model_name": model,
        "dispatch_capabilities": "concurrent_engine_sync",
        "engine_type": "vllm",
        "instances": [
            {
                "id": 1,
                "role": "prefill",
                "endpoints": [{"id": 0, "address": f"{prefill_host}:{prefill_port}"}],
            },
            {
                "id": 2,
                "role": "decode",
                "endpoints": [{"id": 0, "address": f"{decode_host}:{decode_port}"}],
            },
        ],
    }
    readiness_url = f"http://127.0.0.1:{mgmt_port}/readiness"
    refresh_url = f"http://127.0.0.1:{mgmt_port}/instances/refresh"

    process = _start_coordinator(config_path)
    try:
        # Coordinator 刚启动且尚未纳管完整 P+D 拓扑，本场景会进入未就绪分支。
        _wait_for_readiness(readiness_url, False)

        # SET 只纳管 Prefill 实例，本场景仍缺少 Decode 实例，因此保持未就绪。
        prefill_only = {**topology, "instances": [topology["instances"][0]]}
        assert _json_request(refresh_url, prefill_only)[0] == 200
        _wait_for_readiness(readiness_url, False)

        # ADD 仅把已在线的 Decode 实例加入 Coordinator 纳管，本场景不会启动 Decode 进程。
        add_decode = {**topology, "event": "add", "instances": [topology["instances"][1]]}
        assert _json_request(refresh_url, add_decode)[0] == 200

        # P+D 均已纳管，本场景会进入就绪分支。
        _wait_for_readiness(readiness_url, True)
        infer_status, _ = _json_request(
            f"http://127.0.0.1:{infer_port}/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
            },
        )
        assert infer_status == 200

        # DEL 仅将 Decode 实例踢出 Coordinator 纳管，本场景不会停止或销毁 Decode 进程。
        delete_decode = {**topology, "event": "del", "instances": [topology["instances"][1]]}
        assert _json_request(refresh_url, delete_decode)[0] == 200

        # 缺少受纳管的 Decode 实例，本场景会重新进入未就绪分支。
        _wait_for_readiness(readiness_url, False)

        # 再次 ADD 同一个 Decode 实例，本场景复用仍在线的外部进程并恢复调度。
        assert _json_request(refresh_url, add_decode)[0] == 200
        _wait_for_readiness(readiness_url, True)

        # 删除后重新加入仍可完成推理，证明 DEL 没有真实下线外部 Decode 实例。
        infer_status, _ = _json_request(
            f"http://127.0.0.1:{infer_port}/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
            },
        )
        assert infer_status == 200
    finally:
        _stop_coordinator(process)

    process = _start_coordinator(config_path)
    try:
        _wait_for_readiness(readiness_url, False)
        assert _json_request(refresh_url, topology)[0] == 200
        _wait_for_readiness(readiness_url, True)
    finally:
        _stop_coordinator(process)
