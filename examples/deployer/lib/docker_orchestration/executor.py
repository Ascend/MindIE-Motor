# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Bounded OpenSSH transport for a short-lived, JSON-in/JSON-out worker."""

from __future__ import annotations

# Exact JSON scalar checks intentionally reject bool, which is an int subclass.
# pylint: disable=unidiomatic-typecheck

import json
import math
import re
import shlex
import subprocess
import tempfile
import threading
from pathlib import PurePosixPath
from typing import Any


_MAX_REQUEST = 4 * 1024 * 1024
_MAX_RESPONSE = 4 * 1024 * 1024
_MAX_DIAGNOSTICS = 256 * 1024
_READ_ACTIONS = frozenset({"observe", "plan", "inspect", "status", "operation", "logs"})
_TARGET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]{0,252}")


class ExecutorError(RuntimeError):
    """An execution failure whose remote side effects may be indeterminate."""

    def __init__(self, message: str, *, outcome_unknown: bool = False, receipt: dict | None = None) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
        self.receipt = receipt


def _pairs(values: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate worker response key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite worker JSON")


def _refusal_receipt(response: dict, payload: dict) -> dict | None:
    if (
        response.get("ok") is not False
        or response.get("phase") != "failed_no_effect"
        or response.get("executor_terminal") is not True
        or response.get("may_have_effects") is not False
        or response.get("outcome_unknown") is not False
    ):
        return None
    if type(response.get("generation")) is not int or type(payload.get("generation")) is not int:
        return None
    identifiers = {
        "operation_id": payload.get("operation_id"),
        "spec_digest": payload.get("spec_digest"),
        "slot_id": payload.get("slot", {}).get("id"),
        "generation": payload.get("generation"),
        "cluster": payload.get("cluster"),
        "host_id": payload.get("host", {}).get("id"),
        "machine_id": payload.get("expected_machine_id"),
        "daemon_id": payload.get("expected_daemon_id"),
    }
    if any(value is None or response.get(key) != value for key, value in identifiers.items()):
        return None
    if payload.get("action") != "operation" and response.get("action") != payload.get("action"):
        return None
    if response.get("action") not in {"deploy", "restart", "stop", "remove"}:
        return None
    if response.get("container_ids") != payload.get("container_ids", []):
        return None
    return dict(response)


def _read_pipe(pipe: Any, limit: int, output: dict, key: str, process: subprocess.Popen) -> None:
    chunks = []
    count = 0
    try:
        while block := pipe.read(16384):
            count += len(block)
            if count > limit:
                output["overflow"] = True
                process.kill()
                return
            chunks.append(block)
        output[key] = b"".join(chunks)
    except OSError:
        output["overflow"] = True
    finally:
        pipe.close()


class Executor:
    """Use existing OpenSSH host trust and credentials without a resident service."""

    def call(self, host: dict, request: dict, *, timeout: float = 60) -> dict:
        """Execute one request; never automatically retry an uncertain mutation."""
        if not isinstance(request, dict) or not isinstance(host, dict):
            raise ExecutorError("Host and request must be objects")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 3600
        ):
            raise ExecutorError("Invalid execution timeout")
        target = host.get("ssh_target")
        repository = host.get("repo_dir")
        interpreter = host.get("python_executable", "python3")
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise ExecutorError("ssh_target must be a trusted SSH alias or host")
        if (
            not isinstance(repository, str)
            or not repository.startswith("/")
            or any(ord(char) < 32 for char in repository)
            or ".." in PurePosixPath(repository).parts
        ):
            raise ExecutorError("repo_dir must be an absolute, non-traversing POSIX path")
        if not isinstance(interpreter, str):
            raise ExecutorError("python_executable must be python3 or a trusted absolute POSIX path")
        if interpreter != "python3":
            invalid_path = (
                not interpreter.startswith("/") or interpreter == "/" or ".." in PurePosixPath(interpreter).parts
            )
            invalid_text = any(ord(char) < 32 or ord(char) == 127 for char in interpreter)
            if invalid_path or invalid_text:
                raise ExecutorError("python_executable must be python3 or a trusted absolute POSIX path")
        payload = dict(request)
        payload["host"] = dict(host)
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ExecutorError("Request is not finite JSON") from error
        if len(encoded) > _MAX_REQUEST:
            raise ExecutorError("Worker request exceeds the size limit")
        command = shlex.join(
            [
                interpreter,
                "-B",
                str(PurePosixPath(repository) / "examples/deployer/docker_cluster.py"),
                "worker",
            ]
        )
        argv = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "--",
            target,
            command,
        ]
        uncertain = payload.get("action") not in _READ_ACTIONS
        output: dict = {}
        with tempfile.TemporaryFile() as input_file:
            input_file.write(encoded)
            input_file.seek(0)
            try:
                # Explicit bounded wait and timeout kill avoid an unbounded context-manager wait.
                process = subprocess.Popen(  # nosec B603  # pylint: disable=consider-using-with
                    argv,
                    stdin=input_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as error:
                raise ExecutorError("OpenSSH could not be started") from error
            readers = [
                threading.Thread(
                    target=_read_pipe, args=(process.stdout, _MAX_RESPONSE, output, "stdout", process), daemon=True
                ),
                threading.Thread(
                    target=_read_pipe,
                    args=(process.stderr, _MAX_DIAGNOSTICS, output, "stderr", process),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise ExecutorError(
                    "SSH deadline exceeded; query operation before retry", outcome_unknown=uncertain
                ) from error
            finally:
                for reader in readers:
                    reader.join(timeout=5)
            if output.get("overflow") or any(reader.is_alive() for reader in readers):
                raise ExecutorError("SSH output exceeded its bounds", outcome_unknown=uncertain)
        try:
            response = json.loads(
                output.get("stdout", b"").decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
            )
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise ValueError("Missing worker response status")
        except (ValueError, UnicodeError) as error:
            raise ExecutorError("Worker returned an invalid JSON response", outcome_unknown=uncertain) from error
        if not response["ok"]:
            receipt = _refusal_receipt(response, payload)
            raise ExecutorError(
                str(response.get("error", "Remote worker refused the request"))[:512],
                outcome_unknown=bool(response.get("outcome_unknown") or response.get("may_have_effects")),
                receipt=receipt,
            )
        if process.returncode != 0:
            raise ExecutorError("SSH worker exited without a confirmed successful result", outcome_unknown=uncertain)
        return response
