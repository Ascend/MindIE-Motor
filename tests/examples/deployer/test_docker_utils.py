# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Docker 一键 / --create / --start 的必要契约。"""

# Leftover Popen is reaped in finally; with-statement would wait() too early.
# pylint: disable=consider-using-with

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import docker_deploy  # noqa: E402
import lib.constant as C  # noqa: E402
import lib.docker_utils as D  # noqa: E402
import lib.in_place_run as in_place_run  # noqa: E402


def _pd_config():
    return {
        "motor_deploy_config": {
            "p_instances_num": 1,
            "d_instances_num": 1,
            "p_pod_npu_num": 1,
            "d_pod_npu_num": 1,
            "image_name": "img",
            "hardware_type": "800I_A3",
            "deploy_mode": "single_container",
        },
        "motor_coordinator_config": {"api_config": {}},
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "engine_config": {"tensor_parallel_size": 1, "data_parallel_size": 1, "pipeline_parallel_size": 1},
        },
        "motor_engine_decode_config": {
            "engine_type": "vllm",
            "engine_config": {"tensor_parallel_size": 1, "data_parallel_size": 1, "pipeline_parallel_size": 1},
        },
    }


def _parse(*argv):
    with patch.object(sys, "argv", ["docker_deploy.py", *argv]):
        return docker_deploy.parse_arguments()


class DockerDeployTests(unittest.TestCase):
    def test_import_without_yaml(self):
        script = (
            "import sys\n"
            "sys.modules['yaml'] = None\n"
            f"sys.path.insert(0, {str(DEPLOYER_ROOT)!r})\n"
            "import docker_deploy\n"
            "import lib.docker_utils\n"
            "assert 'lib.utils' not in sys.modules\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_create_start_and_roles(self):
        created = _parse("--config_dir", "/mnt/motor", "--container-name", "motor-ctrl", "--create")
        self.assertTrue(created.create)
        self.assertFalse(created.start)

        started = _parse(
            "--config_dir",
            "/mnt/motor",
            "--role",
            "prefill",
            "--instance-name",
            "p0",
            "--start",
            "--pod-ip",
            "10.0.0.1",
            "--nic-name",
            "eth0",
        )
        self.assertTrue(started.start)
        self.assertEqual(started.job_name, "p0")
        self.assertEqual(
            _parse("--config_dir", "/mnt/motor", "--role", "coordinator,controller").role, "coordinator_controller"
        )

        with patch.object(sys, "stderr"):
            with self.assertRaises(SystemExit):
                _parse("--config_dir", "/mnt/motor", "--role", "coordinator,controller,prefill", "--start")

    def test_templates_and_devices(self):
        a2, ctrl = C.ENTER_DOCKER_RUN_A2, C.ENTER_DOCKER_RUN_CTRL
        self.assertIn("--device /dev/davinci7", a2)
        self.assertNotIn("--device", ctrl)
        self.assertIs(docker_deploy.enter_docker_run_template("prefill", "800I_A2"), a2)
        self.assertIs(docker_deploy.enter_docker_run_template("coordinator_controller", "800I_A2"), ctrl)
        filtered = docker_deploy.apply_enter_devices(a2, "0,3", attach_npu=True)
        self.assertIn("--device /dev/davinci0", filtered)
        self.assertNotIn("--device /dev/davinci1", filtered)

    def test_identity_and_workspace(self):
        with patch.object(D, "ip_from_nic", return_value="10.0.0.1"):
            ctrl = D.resolve_runtime_identity(
                _pd_config(),
                role="coordinator,controller",
                job_name=None,
                pod_ip="10.0.0.1",
                coordinator_ip=None,
                controller_ip=None,
                kv_store_ip=None,
                kv_store_enabled=False,
                nic_name="eth0",
            )
        self.assertEqual(ctrl.role, "coordinator_controller")
        self.assertFalse(ctrl.attach_npu)
        with self.assertRaisesRegex(ValueError, "cannot share a container"):
            D.normalize_docker_role("coordinator_controller+prefill")
        self.assertEqual(
            docker_deploy.default_in_place_workspace("/opt/examples/deployer", None, None),
            os.path.abspath("/opt/examples/motor_workspace/single"),
        )

    def test_one_click_refuses_existing_name(self):
        command = docker_deploy.attach_one_click_command(
            C.ENTER_DOCKER_RUN_A2, "python3 /opt/examples/deployer/docker_deploy.py --start"
        )
        self.assertIn("--start", command)
        self.assertIn("exec bash", command)
        args = type(
            "A",
            (),
            {
                "config_dir": "/mnt/motor",
                "container_name": "motor-p0",
                "role": None,
                "devices": None,
                "pod_ip": "10.0.0.8",
                "nic_name": "eth0",
            },
        )()
        with (
            patch.object(
                docker_deploy,
                "resolve_enter_env",
                return_value={"NAME": "motor-p0", "IMAGE": "img", "WEIGHT": "/w", "EXAMPLES": "/e"},
            ),
            patch.object(D, "docker_available", return_value=True),
            patch.object(D, "container_exists", return_value=True),
            patch.object(os, "execvpe") as execvpe,
        ):
            self.assertEqual(docker_deploy._run_enter(args, "/tmp/deployer"), 1)
        execvpe.assert_not_called()

    def test_signal_services_only_kills_mocked_pids(self):
        leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "vllm serve leftover"])
        unrelated = subprocess.Popen(["sleep", "30"])
        try:
            with (
                patch.object(in_place_run, "_in_docker", return_value=True),
                patch.object(in_place_run, "_iter_pids", return_value=[leftover.pid]),
            ):
                in_place_run._signal_services(signal.SIGKILL, {os.getpid(), os.getppid(), 1})
            leftover.wait(timeout=2)
            self.assertIsNotNone(leftover.returncode)
            self.assertIsNone(unrelated.poll())
        finally:
            for proc in (leftover, unrelated):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)

    def test_run_in_place_reaps_orphan_from_iter_pids(self):
        leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "vllm serve leftover"])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                start = os.path.join(tmp, "start_motor.sh")
                log_path = os.path.join(tmp, "run.log")
                Path(start).write_text("#!/bin/bash\necho motor-ok\nexit 0\n", encoding="utf-8")
                os.chmod(start, 0o755)
                with (
                    patch.object(in_place_run, "_in_docker", return_value=True),
                    patch.object(in_place_run, "_iter_pids", return_value=[leftover.pid]),
                ):
                    rc = in_place_run.run_in_place(start, log_path, "restart-cmd")
                self.assertEqual(rc, 0)
            leftover.wait(timeout=2)
            self.assertIsNotNone(leftover.returncode)
        finally:
            if leftover.poll() is None:
                leftover.kill()
                leftover.wait(timeout=2)
