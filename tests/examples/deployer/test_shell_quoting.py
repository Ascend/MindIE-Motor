# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE_ROOT = Path(os.environ.get("MOTOR_SHELL_SOURCE", Path(__file__).resolve().parents[3]))
DEPLOYER_ROOT = SOURCE_ROOT / "examples" / "deployer"
CAPTURE_PYTHON = '''
python3() {
    if [[ "${2:-}" == ccae_reporter.run ]]; then return 0; fi
    printf '%s\\0' "$@" > "$CAPTURE"
}
'''
COMMON_SETUP = "set_common_env() { :; }; source \"$1\"\n"


def run_shell(tmp_path, command, *args, **env_overrides):
    """Run only shell orchestration; model processes are replaced with argv recorders."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    capture = tmp_path / "argv"
    env = (
        os.environ
        | {
            "CAPTURE": capture.as_posix(),
            "CONFIGMAP_PATH": (tmp_path / "config map[1]").as_posix(),
            "CONFIG_PATH": (tmp_path / "config path[1]").as_posix(),
            "ROLE": "controller",
            "ENABLE_GEN_CERT": "",
            "SAVE_CORE_DUMP_FILE_ENABLE": "",
            "MOTOR_INSTANCE_CONFIG_ADAPTER": "",
            "MOTOR_INSTANCE_PORTS_ENABLE": "0",
        }
        | env_overrides
    )
    shell_args = shlex.join(arg.as_posix() if isinstance(arg, Path) else str(arg) for arg in args)
    result = subprocess.run(
        [bash, "-c", "set -- " + shell_args + "\n" + CAPTURE_PYTHON + command, "test"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    argv = capture.read_bytes().decode().split("\0")[:-1] if capture.exists() else []
    return result, argv


@pytest.mark.parametrize("role", ["controller", "pre fill", "role*", ""])
@pytest.mark.parametrize("explicit_role", [False, True])
def test_probe_preserves_argument_boundaries(tmp_path, role, explicit_role):
    # These matching names make an accidental glob expansion observable.
    (tmp_path / "role-match").touch()
    matching_config = tmp_path / "config map1"
    matching_config.mkdir()
    (matching_config / "probe.py").touch()
    args = ["readiness", role] if explicit_role else ["readiness"]
    result, argv = run_shell(
        tmp_path,
        'source "$1" "${@:2}"',
        DEPLOYER_ROOT / "probe/probe.sh",
        *args,
        ROLE=role,
    )
    assert result.returncode == 0, result.stderr
    assert argv == [(tmp_path / "config map[1]/probe.py").as_posix(), role, "readiness"]


def test_probe_rejects_missing_type_without_starting_python(tmp_path):
    result, argv = run_shell(tmp_path, 'script=$1; shift; source "$script"', DEPLOYER_ROOT / "probe/probe.sh")
    assert result.returncode == 1
    assert argv == []


@pytest.mark.parametrize("single_container", [False, True])
def test_controller_preserves_config_path(tmp_path, single_container):
    config = tmp_path / "user config[1].json"
    config.write_text('{"p_instances_num": 0, "d_instances_num": 0}', encoding="utf-8")
    setup = """
set_controller_env() { :; }; setup_motor_log_path() { :; }
gen_ranktable_config() { :; }; set_cann_env() { :; }; is_a5_hardware() { return 1; }
apply_patches() { :; }; set_coordinator_env() { :; }; motor_track_helper() { :; }
motor_track_child() { :; }; set_prefill_env() { :; }; set_decode_env() { :; }
motor_supervise_children() { wait; }
"""
    # In the single-container script several mocked Python jobs run concurrently.
    capture_controller = CAPTURE_PYTHON.replace(
        'if [[ "${2:-}" == ccae_reporter.run ]]',
        'if [[ "${2:-}" != motor.controller.main ]]',
    )
    filename = "all_combine_in_single_container.sh" if single_container else "controller.sh"
    result, argv = run_shell(
        tmp_path,
        setup + capture_controller + 'source "$1"',
        DEPLOYER_ROOT / "startup/roles" / filename,
        ROLE="SINGLE_CONTAINER" if single_container else "controller",
        USER_CONFIG_PATH=config.as_posix(),
        POD_IP="127.0.0.1",
        KV_STORE_BACKEND="",
    )
    assert result.returncode == 0, result.stderr
    assert argv == ["-m", "motor.controller.main", "--config", config.as_posix()]


@pytest.mark.parametrize("library_path", ["", "/tmp/library with spaces/*:/tmp/next"])
def test_common_creates_directory_with_options_first_and_preserves_kv_path(tmp_path, library_path):
    setup = '''
mkdir() { printf '%s\\0' "$@" > "$MKDIR_CAPTURE"; command mkdir "$@"; }
'''
    mkdir_capture = tmp_path / "mkdir-argv"
    library_capture = tmp_path / "library-path"
    result, argv = run_shell(
        tmp_path,
        setup + COMMON_SETUP + 'gen_kv_store_config; printf %s "$LD_LIBRARY_PATH" > "$LIBRARY_CAPTURE"',
        DEPLOYER_ROOT / "startup/common.sh",
        MKDIR_CAPTURE=mkdir_capture.as_posix(),
        LIBRARY_CAPTURE=library_capture.as_posix(),
        LD_LIBRARY_PATH=library_path,
        KVS_MASTER_SERVICE="127.0.0.1",
    )
    assert result.returncode == 0, result.stderr
    config = tmp_path / "config path[1]"
    assert config.is_dir()
    assert library_capture.read_text(encoding="utf-8") == (
        "/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake:" + library_path
    )
    assert mkdir_capture.read_bytes().decode().split("\0")[:-1] == ["-p", config.as_posix()]
    assert argv == [
        (tmp_path / "config map[1]/mooncake_config.py").as_posix(),
        "pool",
        (config / "kv_cache_store_config.json").as_posix(),
        (tmp_path / "config map[1]/user_config.json").as_posix(),
    ]


@pytest.mark.parametrize("filename", ["run_dp_template_prefill.sh", "run_dp_template_decode.sh"])
def test_config_templates_keep_empty_and_pattern_arguments(tmp_path, filename):
    (tmp_path / "match-me").touch()
    setup = 'vllm() { printf \'%s\\0\' "$@" > "$CAPTURE"; }\n'
    values = ["", "2", "0", "address with spaces", "*", "1"]
    result, argv = run_shell(
        tmp_path,
        setup + 'source "$1" "${@:2}"',
        DEPLOYER_ROOT / "config_tool" / filename,
        "0",
        *values,
    )
    assert result.returncode == 0, result.stderr
    options = [
        "--port",
        "--data-parallel-size",
        "--data-parallel-rank",
        "--data-parallel-address",
        "--data-parallel-rpc-port",
        "--tensor-parallel-size",
    ]
    assert [argv[argv.index(option) + 1] for option in options] == values


def test_slurm_passes_boot_path_as_data_to_inner_bash(tmp_path):
    configmap = tmp_path / 'config map; echo unexpected'
    configmap.mkdir()
    (configmap / "boot.sh").write_text('printf boot-ok > "$CAPTURE"\n', encoding="utf-8")
    # Exercise the container command only, without starting a Slurm job or a container.
    command = (
        "apptainer exec \\\n"
        + (DEPLOYER_ROOT / "slurm_job.sh").read_text(encoding="utf-8").split("\napptainer exec \\\n", 1)[1]
    )
    setup = '''
RUNTIME_ENV=(); INSTANCE_NAME=test
apptainer() { while [[ "$1" != bash ]]; do shift; done; "$@"; }
'''
    result, _ = run_shell(tmp_path, setup + command, CONFIGMAP_PATH=configmap.as_posix())
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "argv").read_text(encoding="utf-8") == "boot-ok"


@pytest.mark.parametrize("python_exit_code", [0, 1, 17])
def test_prestop_signals_node_manager_and_preserves_python_exit_code(tmp_path, python_exit_code):
    script = (DEPLOYER_ROOT / "prestop/prestop.sh").read_text(encoding="utf-8")
    redirect = "exec >> /proc/1/fd/1 2>&1"
    assert script.count(redirect) == 1
    # Redirect container logging to a temporary file; leave the stop sequence unchanged.
    script_path = tmp_path / "prestop under test.sh"
    script_path.write_text(
        script.replace(redirect, 'exec >> "$PRESTOP_LOG" 2>&1'),
        encoding="utf-8",
    )
    kill_capture = tmp_path / "kill-argv"
    setup = """
ps() { printf '424242 MindIE-Motor::NodeManager\\n'; }
python3() { printf '%s\\0' "$@" > "$CAPTURE"; return "$PYTHON_EXIT_CODE"; }
kill() { printf '%s\\0' "$@" > "$KILL_CAPTURE"; }
"""
    result, argv = run_shell(
        tmp_path,
        setup + 'source "$1" "${@:2}"',
        script_path,
        "--max-wait",
        "0",
        PYTHON_EXIT_CODE=str(python_exit_code),
        PRESTOP_LOG=(tmp_path / "prestop.log").as_posix(),
        KILL_CAPTURE=kill_capture.as_posix(),
    )
    assert result.returncode == python_exit_code, result.stderr
    assert argv == [(tmp_path / "config map[1]/prestop.py").as_posix(), "--max-wait", "0"]
    assert kill_capture.exists(), "PreStop exited without sending SIGTERM to NodeManager"
    assert kill_capture.read_bytes().decode().split("\0")[:-1] == ["-TERM", "424242"]
