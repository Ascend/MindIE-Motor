# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Run start_motor.sh under --start and stop Motor/vLLM on this TTY.

Stop is two steps, same shape as upstream vLLM:

1. SIGTERM start_motor.sh and any leftover Motor/vLLM processes in this container
2. after a short grace, SIGKILL those process trees

vLLM is started with start_new_session, so it can outlive NodeManager and be
reparented to PID 1. Matching cmdline (not PID 1, not every process) is how
Ctrl+C and the next --start both reap it. After that pass, reap Python /
Motor leftovers whose parent is already container PID 1 (plain-python workers
have no cmdline marker). Host has no /.dockerenv, so this never runs there.
docker exec shells are plain bash and are left alone.

stdout is a pty so Motor line-buffers and this TTY shows docker -it logs.
"""

from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _iter_pids() -> list[int]:
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    return [int(name) for name in names if name.isdigit()]


def _in_docker() -> bool:
    """True in a container. Survives exec bash and --create; false on the host."""
    if os.environ.get("MOTOR_INPLACE_AS_DOCKER") == "1":
        return True
    return Path("/.dockerenv").exists()


def _proc_exists(pid: int) -> bool:
    return pid > 1 and Path(f"/proc/{pid}").exists()


def _cmdline(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


_SERVICE_MARKERS = (
    b"vllm serve",
    b"VLLM::EngineCore",
    b"-m motor.",
    b"start_motor.sh",
    b"ccae_reporter",
    b"SchedulerServer",
    b"multiprocessing.resource_tracker",
)


def _is_service_cmd(cmd: bytes) -> bool:
    if b"docker_deploy.py" in cmd:
        return False
    return any(marker in cmd for marker in _SERVICE_MARKERS)


def _service_pids(protected: set[int]) -> list[int]:
    found: list[int] = []
    for pid in _iter_pids():
        if pid <= 1 or pid in protected:
            continue
        if _is_service_cmd(_cmdline(pid)):
            found.append(pid)
    return found


def _signal_services(sig: int, protected: set[int]) -> None:
    """Reap Motor/vLLM leftovers. Safe after exec bash; skips docker exec bash."""
    if not _in_docker():
        return
    for pid in _service_pids(protected):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _ppid(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return 0
    return 0


def _allow_pid1_reap() -> bool:
    """Only inside a real container PID namespace. Never against host systemd."""
    return Path("/.dockerenv").exists()


def _is_python_proc(pid: int) -> bool:
    try:
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe")).lower()
    except OSError:
        exe = ""
    if "python" in exe:
        return True
    argv0 = _cmdline(pid).split(b"\x00", 1)[0].lower()
    return b"python" in argv0


def _pid1_orphan_pids(protected: set[int]) -> list[int]:
    skip = set(protected) | {os.getpid(), os.getppid(), 1}
    found: list[int] = []
    for pid in _iter_pids():
        if pid <= 1 or pid in skip:
            continue
        if _ppid(pid) != 1:
            continue
        cmd = _cmdline(pid)
        if b"docker_deploy.py" in cmd:
            continue
        if _is_python_proc(pid) or _is_service_cmd(cmd):
            found.append(pid)
    return found


def _reap_pid1_orphans(protected: set[int]) -> None:
    """SIGKILL engines already reparented to container PID 1. Leaves exec bash."""
    if not _in_docker() or not _allow_pid1_reap():
        return
    skip = set(protected) | {os.getpid(), os.getppid(), 1}
    for _ in range(3):
        orphans = _pid1_orphan_pids(skip)
        if not orphans:
            return
        for pid in orphans:
            kill_process_tree(pid, skip)


def _grace_sec() -> float:
    raw = (os.environ.get("MOTOR_INPLACE_STOP_GRACE_SEC") or "5").strip()
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 5.0


def _children(pid: int) -> list[int]:
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(x) for x in text.split() if x.isdigit()]


def _expand_tree(roots: set[int]) -> set[int]:
    found = set(roots)
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in _children(pid):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _descendants(pid: int) -> list[int]:
    found: list[int] = []
    seen = {pid}
    stack = [pid]
    while stack:
        current = stack.pop()
        for child in _children(current):
            if child not in seen:
                seen.add(child)
                found.append(child)
                stack.append(child)
    return found


def kill_process_tree(pid: int, protected: set[int] | None = None) -> None:
    """SIGKILL descendants then *pid*. Same shape as vllm.utils.system_utils."""
    skip = protected or set()
    if pid <= 1 or pid in skip:
        return
    for child in _descendants(pid):
        if child <= 1 or child in skip:
            continue
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _signal_container_others(sig: int, protected: set[int]) -> None:
    _signal_services(sig, protected)


def run_in_place(start_motor: str, log_path: str, restart_cmd: str, *, grace: float | None = None) -> int:
    grace_s = _grace_sec() if grace is None else max(0.1, float(grace))
    protected = {os.getpid(), os.getppid(), 1}
    # Next --start after exec bash must clear last run's orphan vLLM first.
    _signal_services(signal.SIGKILL, protected)
    _reap_pid1_orphans(protected)
    known: set[int] = set()
    lock = threading.Lock()
    interrupted = 0
    force_now = threading.Event()
    with open(log_path, "wb") as log_fh:
        # A pipe is not a TTY: Motor line-buffers / may skip stdout, and the copy
        # thread can stall before it ever writes the log file. Give start_motor a
        # pty so logs show up on this terminal the same way as docker -it.
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.Popen(  # nosec B607  # pylint: disable=consider-using-with
            ["bash", os.path.abspath(start_motor)],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=env,
        )
        os.close(slave_fd)
        with lock:
            known.add(proc.pid)

        def snapshot() -> set[int]:
            with lock:
                known.update(_expand_tree(set(known) | {proc.pid}))
                return set(known)

        def graceful() -> None:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass
            _signal_container_others(signal.SIGTERM, protected)

        def hard_kill() -> None:
            snapshot()
            if proc.poll() is None:
                kill_process_tree(proc.pid, protected)
            for pid in snapshot():
                if pid <= 1 or pid in protected:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            _signal_container_others(signal.SIGKILL, protected)
            _reap_pid1_orphans(protected)
            if proc.poll() is None:
                proc.kill()

        def track() -> None:
            while proc.poll() is None:
                snapshot()
                time.sleep(0.2)
            snapshot()

        def pump() -> None:
            try:
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0.2)
                    if not ready:
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    log_fh.write(chunk)
                    log_fh.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            except OSError:
                pass
            finally:
                try:
                    while True:
                        ready, _, _ = select.select([master_fd], [], [], 0)
                        if not ready:
                            break
                        leftover = os.read(master_fd, 65536)
                        if not leftover:
                            break
                        log_fh.write(leftover)
                        log_fh.flush()
                        sys.stdout.buffer.write(leftover)
                        sys.stdout.buffer.flush()
                except OSError:
                    pass
                try:
                    os.close(master_fd)
                except OSError:
                    pass

        def on_stop(_signum=None, _frame=None) -> None:
            nonlocal interrupted
            interrupted += 1
            if interrupted == 1:
                graceful()

                def later() -> None:
                    if not force_now.wait(grace_s):
                        hard_kill()

                threading.Thread(target=later, daemon=True).start()
            else:
                force_now.set()
                hard_kill()

        threading.Thread(target=track, daemon=True).start()
        pump_t = threading.Thread(target=pump, daemon=True)
        pump_t.start()
        old_int = signal.signal(signal.SIGINT, on_stop)
        old_term = signal.signal(signal.SIGTERM, on_stop)
        try:
            while proc.poll() is None and _proc_exists(proc.pid):
                try:
                    time.sleep(0.1)
                except InterruptedError:
                    continue
            rc = proc.returncode if proc.returncode is not None else 0
            hard_kill()
            pump_t.join(timeout=1.0)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        print("\n容器内服务已终止，可执行以下命令重新部署服务。", flush=True)
        print(restart_cmd, flush=True)
        print(f"运行日志：{log_path}", flush=True)
        if interrupted:
            return 143
        return rc if rc >= 0 else 1
