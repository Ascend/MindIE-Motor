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
            patch.object(D, "image_exists", return_value=True),
            patch.object(D, "container_exists", return_value=True),
            patch.object(docker_deploy, "_hardware_type_from_args", return_value="800I_A2"),
            patch.object(docker_deploy, "enter_docker_run_template", return_value=C.ENTER_DOCKER_RUN_CTRL),
            patch.object(docker_deploy, "_dshm_size_from_args", return_value=None),
            patch.object(docker_deploy, "apply_enter_devices", side_effect=lambda template, *_a, **_k: template),
            patch.object(docker_deploy, "insert_create_binds", side_effect=lambda template, *_a, **_k: template),
            patch.object(docker_deploy, "apply_enter_weight", side_effect=lambda template, *_a, **_k: template),
            patch.object(docker_deploy, "apply_enter_shm", side_effect=lambda template, *_a, **_k: template),
            patch.object(docker_deploy, "_require_host_binds"),
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(D, "remove_container") as remove,
            patch.object(os, "execvpe") as execvpe,
        ):
            self.assertEqual(docker_deploy._run_enter(args, "/tmp/deployer"), 1)
        remove.assert_not_called()
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

    def test_start_devices_marker_matches_script_export(self):
        self.assertEqual(D.start_devices_marker("2, 3"), "2,3")
        self.assertEqual(D.start_devices_marker("  "), "")
        self.assertEqual(D.isolate_start_devices("prefill", "0,1"), "0,1")
        self.assertEqual(D.isolate_start_devices("coordinator,controller", "0,1"), "")
        self.assertEqual(D.isolate_start_devices("decode", None), "")
        lines = "\n".join(D._device_env_lines("0,1"))
        self.assertIn('export MOTOR_START_DEVICES="0,1"', lines)

    def test_run_in_place_only_devices_does_not_reap_sibling(self):
        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "vllm serve sibling"])
        mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "vllm serve mine"])
        try:

            def fake_environ(pid):
                if pid == sibling.pid:
                    return b"MOTOR_START_DEVICES=0,1\x00"
                if pid == mine.pid:
                    return b"MOTOR_START_DEVICES=2,3\x00"
                return b""

            with tempfile.TemporaryDirectory() as tmp:
                start = os.path.join(tmp, "start_motor.sh")
                log_path = os.path.join(tmp, "run.log")
                Path(start).write_text("#!/bin/bash\necho motor-ok\nexit 0\n", encoding="utf-8")
                os.chmod(start, 0o755)
                with (
                    patch.object(in_place_run, "_in_docker", return_value=True),
                    patch.object(in_place_run, "_iter_pids", return_value=[sibling.pid, mine.pid]),
                    patch.object(in_place_run, "_proc_environ", side_effect=fake_environ),
                    patch.dict(os.environ, {}, clear=False),
                ):
                    os.environ.pop("MOTOR_START_DEVICES", None)
                    rc = in_place_run.run_in_place(start, log_path, "restart-cmd", only_devices="2,3")
                self.assertEqual(rc, 0)
            mine.wait(timeout=2)
            self.assertIsNotNone(mine.returncode)
            self.assertIsNone(sibling.poll())
        finally:
            os.environ.pop("MOTOR_START_DEVICES", None)
            for proc in (sibling, mine):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)

    def test_start_in_place_forwards_only_devices(self):
        with patch.object(docker_deploy, "run_in_place", return_value=0) as run:
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "start_motor.sh").write_text("#!/bin/bash\n", encoding="utf-8")
                with patch.object(docker_deploy, "_new_run_log_path", return_value=os.path.join(tmp, "run.log")):
                    rc = docker_deploy._start_in_place(tmp, only_devices="4,5")
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.kwargs.get("only_devices"), "4,5")

    def test_run_passes_isolate_marker_to_start_in_place(self):
        identity = D.DockerRuntimeIdentity(
            role="decode",
            job_name="d0",
            pod_ip="10.0.0.8",
            coordinator_ip="10.0.0.1",
            controller_ip="10.0.0.1",
            kv_store_ip="10.0.0.1",
            host_network=True,
            attach_npu=True,
            nic_name="eth0",
        )
        args = type("A", (), {"devices": "2,3", "start": True})()
        with (
            patch.object(docker_deploy, "_prepare_and_render", return_value=(object(), "/ws", identity)),
            patch.object(docker_deploy, "_print_in_place_banner"),
            patch.object(docker_deploy, "_start_in_place", return_value=0) as start,
        ):
            self.assertEqual(docker_deploy.run(args, "/tmp/deployer"), 0)
        self.assertEqual(start.call_args.kwargs.get("only_devices"), "2,3")

    def test_one_click_inner_start_does_not_forward_devices(self):
        args = type(
            "A",
            (),
            {
                "config_dir": "/tmp/cfg",
                "user_config_path": "",
                "env_config_path": "",
                "container_name": "motor-p0",
                "role": "prefill",
                "job_name": "p0",
                "pod_ip": "10.0.0.8",
                "nic_name": "eth0",
                "coordinator_ip": "10.0.0.1",
                "controller_ip": "10.0.0.1",
                "kv_store_ip": None,
                "node_manager_port": 4026,
                "data_parallel_rpc_port": 9000,
                "kv_port": 21000,
                "base_port": 10000,
                "devices": "0,1",
            },
        )()
        inner = docker_deploy.build_in_place_inner_command(args, "/tmp/deployer")
        self.assertIn("--start", inner)
        self.assertNotIn("--devices", inner)
        self.assertIn("--node-manager-port", inner)

    def test_start_script_splits_config_and_cards_per_instance(self):
        params = D.DockerDeployParams(
            image="img",
            weight_mount_path="/w",
            hardware_type="800I_A2",
            is_a5=False,
            required_npu_num=2,
            infer_container_port=1025,
            obs_container_port=1026,
            infer_host_port=1025,
            obs_host_port=1026,
            deploy_mode="infer_service_set",
            kv_store_enabled=False,
        )

        def identity(role, job):
            return D.DockerRuntimeIdentity(
                role=role,
                job_name=job,
                pod_ip="10.0.0.8",
                coordinator_ip="10.0.0.1",
                controller_ip="10.0.0.1",
                kv_store_ip="10.0.0.1",
                host_network=True,
                attach_npu=True,
                nic_name="eth0",
            )

        with patch.object(D, "ip_from_nic", return_value="10.0.0.8"):
            prefill = D.render_start_motor_sh(
                params,
                "/ws/p0/configmap",
                identity=identity("prefill", "p0"),
                container_name="motor-pd",
                devices="0,1",
            )
            decode = D.render_start_motor_sh(
                params,
                "/ws/d0/configmap",
                identity=identity("decode", "d0"),
                container_name="motor-pd",
                devices="2,3",
            )
            untouched = D.render_start_motor_sh(
                params,
                "/ws/p0/configmap",
                identity=identity("prefill", "p0"),
                container_name="motor-p0",
            )
            with self.assertRaises(ValueError):
                D.render_start_motor_sh(
                    params,
                    "/ws/p0/configmap",
                    identity=identity("prefill", "p0"),
                    container_name="motor-pd",
                    devices="0,0",
                )
        self.assertIn('export CONFIG_PATH="/usr/local/Ascend/pyMotor/conf/motor-pd/p0"', prefill)
        self.assertIn('export ASCEND_RT_VISIBLE_DEVICES="0,1"', prefill)
        self.assertIn('export ASCEND_VISIBLE_DEVICES="0,1"', prefill)
        self.assertIn('export CONFIG_PATH="/usr/local/Ascend/pyMotor/conf/motor-pd/d0"', decode)
        self.assertIn('export ASCEND_RT_VISIBLE_DEVICES="2,3"', decode)
        self.assertIn('export CONFIG_PATH="/usr/local/Ascend/pyMotor/conf"', untouched)
        self.assertNotIn("ASCEND_RT_VISIBLE_DEVICES", untouched)

    def test_hccl_visible_devices_follow_runtime_limit(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hccl_tools_under_test",
            DEPLOYER_ROOT / "startup" / "hccl_tools.py",
        )
        hccl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hccl)
        with (
            patch.object(hccl, "_davinci_ids", return_value=["0", "1", "2", "3"]),
            patch.dict(os.environ, {"ASCEND_RT_VISIBLE_DEVICES": "2,3"}, clear=False),
        ):
            self.assertEqual(hccl.get_visible_devices(), ["2", "3"])
        with (
            patch.object(hccl, "_davinci_ids", return_value=["0", "1"]),
            patch.dict(os.environ, {"ASCEND_RT_VISIBLE_DEVICES": "0,2"}, clear=False),
        ):
            with self.assertRaises(RuntimeError):
                hccl.get_visible_devices()
        with (
            patch.object(hccl, "_davinci_ids", return_value=["0", "1", "2", "3"]),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("ASCEND_RT_VISIBLE_DEVICES", None)
            os.environ.pop("ASCEND_VISIBLE_DEVICES", None)
            self.assertEqual(hccl.get_visible_devices(), ["0", "1", "2", "3"])
