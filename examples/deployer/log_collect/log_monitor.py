# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# Copyright Huawei Technologies Co., Ltd. 2026. All rights reserved.
from datetime import datetime, timedelta, timezone
import configparser
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time


UNKNOWN_NODE_NAME = "unknown"
IDLE_EXIT_SECONDS = 5  # exit when no pods in namespace for this duration

# Exponential backoff for pod restarts: when a pod restarts repeatedly within
# BACKOFF_WINDOW_SECONDS, each restart doubles the delay
# (BACKOFF_BASE_SECONDS * 2^n, capped at BACKOFF_MAX_SECONDS).
# After a stable interval without restarts the counter resets.
BACKOFF_WINDOW_SECONDS = 60
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 120

# Configuration parameters, Configured in the 'log_config.ini' file
# The log file size is configured in bytes.
# For ease of reading, you are advised to set the log file size to no more than 100 MB
# and the number of backup log files to no more than 1000.
g_name_space = ""
g_target_log = "./log/"
g_max_log_size = 10000000
g_backup_count = 10


console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logger_monitor = logging.getLogger("logger_monitor")
logger_monitor.setLevel(logging.INFO)
logger_monitor.handlers.clear()
logger_monitor.addHandler(console_handler)


def log_i(msg: str) -> None:
    logger_monitor.info(msg)


def log_w(msg: str) -> None:
    logger_monitor.warning(msg)


def log_e(msg: str) -> None:
    logger_monitor.error(msg)


class LogMonitor:
    # Regex to strip ANSI escape sequences (e.g. \x1b[32m, \x1b[0m)
    # produced by Rust tracing-subscriber / kubectl logs with color.
    _ANSI_ESCAPE_RE = __import__("re").compile(r'\x1b\[[0-9;]*m')

    @classmethod
    def _strip_ansi(cls, line: str) -> str:
        return cls._ANSI_ESCAPE_RE.sub('', line)

    def __init__(self):
        self.encode_type = "utf-8"
        self.cmd_kubectl = "/usr/bin/kubectl"
        self.cmd_awk = "/usr/bin/awk"
        self.thread_name = "thread-log-"

        self.threads = []
        self.exit_flag = threading.Event()
        self._idle = False
        self._logged_save_paths = set()
        # Per pod name: cross-thread log suffix so a new collector after thread exit
        # does not reuse _0 while an older "life" already used lower indices.
        self._pod_log_next_slot: dict[str, int] = {}
        # Incremented whenever a collector thread for this pod exits (any reason).
        self._pod_log_collector_generation: dict[str, int] = {}
        # Per-pod backoff state: {pod_name: (restart_count, first_restart_ts)}
        self._pod_backoff: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _allocate_unique_log_path(pod_name: str, node_name: str, start_index: int) -> tuple[str, int]:
        """Return path and index for the first unused ``{pod}_{node}_{n}.log`` under ``g_target_log``."""
        candidate_log_index = start_index
        while True:
            file_path = os.path.join(g_target_log, f"{pod_name}_{node_name}_{candidate_log_index}.log")
            abs_path = os.path.abspath(os.path.normpath(file_path))
            if not os.path.exists(abs_path):
                return file_path, candidate_log_index
            candidate_log_index += 1

    def _backoff_sleep(self, pod_name: str) -> float:
        """Return the sleep duration after a pod restart, with exponential backoff.

        Within ``BACKOFF_WINDOW_SECONDS``, each restart doubles the delay
        (``BACKOFF_BASE_SECONDS * 2^count``), capped at ``BACKOFF_MAX_SECONDS``.
        After the window expires the counter resets.
        """
        now = time.monotonic()
        prev = self._pod_backoff.get(pod_name)
        if prev is None:
            count, _first_ts = 0, now
        else:
            count, first_ts = prev
            if now - first_ts > BACKOFF_WINDOW_SECONDS:
                count, first_ts = 0, now
            else:
                count += 1

        self._pod_backoff[pod_name] = (count, first_ts)
        delay = min(BACKOFF_BASE_SECONDS * (2**count), BACKOFF_MAX_SECONDS)
        if count > 0:
            log_w(f"{pod_name}: restart backoff count={count}, sleeping {delay}s before re-collecting logs.")
        return delay

    def setup_rotating_logger(self, pod_name: str, log_file: str) -> logging.Logger | None:
        """
        Configure a logger with a rotation function
        Args:
        log_file: Path to the log file
        Returns:
        The configured logger object or None (if creation fails)
        """
        # Create the log directory (if not existing).
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
            except OSError as e:
                log_e(f"Unable to create log directory {log_dir}: {e}")
                return None

        # Creating a Logger
        logger = logging.getLogger(pod_name)
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        # Create a RotatingFileHandler to implement log rotation.
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=g_max_log_size, backupCount=g_backup_count, encoding=self.encode_type
        )

        # Setting the log format (only the original log content is retained)
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)

        # Add a processor to the logger
        logger.addHandler(handler)

        return logger

    def shell_get_pod_node(self, pod_name: str) -> str:
        """
        Get the K8s node name where the specified pod is running.
        :param pod_name: Name of the pod
        :return: Node name, or "unknown" if it cannot be determined
        """
        try:
            with subprocess.Popen(
                [self.cmd_kubectl, 'get', 'pod', pod_name, '-n', g_name_space, '-o', 'jsonpath={.spec.nodeName}'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as kubectl_cmd:
                output, _ = kubectl_cmd.communicate()
                if kubectl_cmd.returncode != 0:
                    return UNKNOWN_NODE_NAME
                node_name = output.decode(self.encode_type).strip()
                return node_name if node_name else UNKNOWN_NODE_NAME
        except Exception as e:
            log_e(f"shell_get_pod_node Exception for {pod_name}: {e}")
            return UNKNOWN_NODE_NAME

    def check_pod_is_running(self, pod_name: str) -> bool | None:
        """
        Probe pod phase via kubectl.

        :return: ``True`` if phase is Running; ``False`` if get succeeded but not Running;
            ``None`` if ``kubectl get pod`` failed (caller should end this collector thread).
        """
        with subprocess.Popen(
            [self.cmd_kubectl, 'get', 'pod', pod_name, '-n', g_name_space, '-o', 'jsonpath={.status.phase}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as kubectl_cmd:
            output, err = kubectl_cmd.communicate()
            if kubectl_cmd.returncode != 0:
                err_msg = err.decode(self.encode_type, errors="ignore").strip()
                log_w(f"{pod_name}: kubectl get pod phase failed (exit={kubectl_cmd.returncode}): {err_msg}")
                return None

            status = output.decode(self.encode_type).strip()
            return status == "Running"

    def shell_get_pod(self) -> list[str] | None:
        """
        Run the kubectl command to obtain the pod list.
        Returns:
        A list of pod names or None (if an error occurs)
        """
        try:
            with (
                subprocess.Popen(
                    [self.cmd_kubectl, 'get', 'pods', '-n', g_name_space, '-o', 'wide'],
                    stdout=subprocess.PIPE,
                ) as kubectl_cmd,
                subprocess.Popen(
                    [self.cmd_awk, 'NR>1 {print $1}'],
                    stdin=kubectl_cmd.stdout,
                    stdout=subprocess.PIPE,
                ) as awk_cmd,
            ):
                kubectl_cmd.stdout.close()
                output, _ = awk_cmd.communicate()
                return output.decode(self.encode_type).strip().splitlines()
        except Exception as e:
            log_e(f"shell_get_pod Exception: {e}")
            return None

    @staticmethod
    def _kubectl_since_time(when: datetime | None = None, lookback_seconds: float = 2.0) -> str:
        """RFC3339 UTC timestamp for ``kubectl logs --since-time`` (small lookback avoids gaps)."""
        ts = (when or datetime.now(timezone.utc)) - timedelta(seconds=lookback_seconds)
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def shell_pull_log(
        self,
        pod_name: str,
        file_path: str,
        interval: float = 0.2,
        since_time: str | None = None,
    ) -> bool:
        """
        Execute the kubectl command to obtain the log.

        :param since_time: If set, pass ``--since-time`` so reconnects only fetch
            new lines (avoids re-dumping the full container history).
        """
        b_write_flag = False
        abs_path = os.path.abspath(os.path.normpath(file_path))

        logger = self.setup_rotating_logger(pod_name, abs_path)
        if logger is None:
            return b_write_flag

        process = None
        try:
            kubectl_args = [
                self.cmd_kubectl,
                'logs',
                '-f',
                '-n',
                g_name_space,
                pod_name,
            ]
            if since_time:
                kubectl_args.extend(['--since-time', since_time])

            # Long-running kubectl logs -f; with-statement is unsuitable
            # because the process is terminated in the finally block.
            process = subprocess.Popen(  # pylint: disable=consider-using-with
                kubectl_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            size = g_max_log_size / 1024 / 1024
            if abs_path not in self._logged_save_paths:
                self._logged_save_paths.add(abs_path)
                log_i(f"{pod_name}: logs save to: {abs_path} (max {size:.1f}MB, keep {g_backup_count} backups)")
            elif since_time:
                log_i(f"{pod_name}: reconnecting logs with --since-time={since_time}")

            # Reads the output in real time and writes it to the log file.
            while not self.exit_flag.is_set():
                line = process.stdout.readline()
                if not line:
                    # Check whether the cmd command exits.
                    if process.poll() is not None:
                        log_i(f"{pod_name} : cmd has exited.")
                        break
                    time.sleep(interval)
                    continue
                b_write_flag = True
                # Remove newlines and ANSI escape codes, then write to log.
                log_line = self._strip_ansi(line.rstrip('\n'))
                logger.info(log_line)
        except Exception as e:
            log_e(f"{pod_name} :Exception: {e}")
        finally:
            # Ensure the child process is terminated
            if process and process.poll() is None:
                process.terminate()
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            log_i(f"{pod_name}: log stream session ended.")
        return b_write_flag

    def pull_log_and_save(self, pod_name: str, interval: float = 3) -> None:
        """
        Collect logs from a specified pod and save them to a file.
        :param pod_name: Name of the pod to collect logs from
        """
        generation_floor = self._pod_log_collector_generation.get(pod_name, 0)
        next_slot = self._pod_log_next_slot.get(pod_name, 0)
        index = max(generation_floor, next_slot)
        node_name = self.shell_get_pod_node(pod_name)
        # Reuse one rotating log file across kubectl stream reconnects; only
        # allocate a new _{n}.log when the collector starts fresh or the pod
        # moves to another node. Otherwise each ~4h apiserver disconnect would
        # re-dump full history into _1/_2/... (and duplicate .log.1 rotations).
        file_path: str | None = None
        allocated_log_index: int | None = None
        since_time: str | None = None
        try:
            while not self.exit_flag.is_set():
                pod_running_state = self.check_pod_is_running(pod_name)
                if pod_running_state is None:
                    log_w(f"{pod_name}: kubectl get pod failed, exiting collector thread.")
                    return
                if not pod_running_state:
                    log_w(f"{pod_name} :The pod is not in the 'Running' state, waiting...")
                    time.sleep(interval)
                    continue
                fetched_node_name = self.shell_get_pod_node(pod_name)
                if fetched_node_name != node_name:
                    log_i(f"{pod_name}: node_name refresh {node_name!r} -> {fetched_node_name!r}")
                    node_name = fetched_node_name
                    # New node => new file name; fetch full history for this placement.
                    if allocated_log_index is not None:
                        index = max(index, allocated_log_index + 1)
                    file_path = None
                    allocated_log_index = None
                    since_time = None
                if file_path is None:
                    file_path, allocated_log_index = self._allocate_unique_log_path(pod_name, node_name, index)
                    if allocated_log_index != index:
                        log_i(
                            f"{pod_name}: log file slot bumped {index} -> {allocated_log_index} "
                            "(target path already exists)."
                        )
                    index = allocated_log_index
                if self.shell_pull_log(pod_name, file_path, since_time=since_time):
                    # Reserve next slot for a future collector generation, but keep
                    # appending to the same file for this thread's reconnects.
                    self._pod_log_next_slot[pod_name] = max(
                        self._pod_log_next_slot.get(pod_name, 0),
                        allocated_log_index + 1,
                    )
                    # Capture before sleep so logs during the pause are still fetched.
                    since_time = self._kubectl_since_time()
                    # Stream reconnect (not a pod crash-loop) — keep a short pause.
                    # Backoff is reserved for cases that start a brand-new file.
                    delay = interval
                    log_i(
                        f"{pod_name}: Log stream ended; pausing {delay:.0f}s before "
                        f"re-checking pod and reopening logs "
                        f"(same file, --since-time={since_time})."
                    )
                    time.sleep(delay)
                else:
                    # No data written — clean up empty file to avoid slot inflation
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                    file_path = None
                    allocated_log_index = None
                    since_time = None
                    # Failed / crash-loop pulls — exponential backoff before retry.
                    delay = self._backoff_sleep(pod_name)
                    log_w(f"{pod_name}: Failed to pull logs; pausing {delay:.0f}s before retry.")
                    time.sleep(delay)
        except Exception as e:
            log_e(f"{pod_name} :Exception: {e}")
        finally:
            self._pod_log_collector_generation[pod_name] = self._pod_log_collector_generation.get(pod_name, 0) + 1

    def monitor_stop(self, file_path: str, interval: float = 1) -> None:
        """
        Periodically check whether the specified file exists in the directory; if it exists, exit the program.
        :param file_path: Path of the file to be searched
        :param interval: Check interval (in seconds)
        """
        while True:
            # 1、Check whether the stop file exists.
            if os.path.exists(file_path):
                log_i(f"The file {file_path} exists, so the program will exit.")
                self.exit_flag.set()
                break
            # 2、Check whether there are any alive threads
            flag = False
            for thread in self.threads:
                if thread.is_alive():
                    flag = True
                    break
            if not flag:
                log_e("All thread have terminated abnormally, so the program will exit.")
                self.exit_flag.set()
                break

            time.sleep(interval)

    def start_log_thread(self) -> bool:
        # 1、Get pod information.
        list_line = self.shell_get_pod()
        if list_line is None:
            log_e("Exiting: failed to get pod list from kubectl.")
            return False
        if not list_line:
            self._idle = True
            return True

        self._idle = False
        dead_log_thread_names = [
            t.name for t in self.threads if (not t.is_alive()) and t.name.startswith(self.thread_name)
        ]
        if dead_log_thread_names:
            log_i(f"Pruned {len(dead_log_thread_names)} finished log collector thread(s): {dead_log_thread_names}.")
        self.threads = [t for t in self.threads if t.is_alive()]

        # 2、Get newly created pods.
        thread_names = [thread.name for thread in self.threads if thread.name.startswith(self.thread_name)]

        list_line = [pod_name for pod_name in list_line if f"{self.thread_name}{pod_name}" not in thread_names]
        if len(list_line) == 0:
            return True

        # 3、Start the thread for collecting logs.
        for pod_name in list_line:
            log_i(f"pod_name: {pod_name}")
            thread = threading.Thread(
                target=self.pull_log_and_save, args=(pod_name,), name=f"{self.thread_name}{pod_name}", daemon=True
            )
            self.threads.append(thread)
            thread.start()
        log_i(f" {len(list_line)} threads have been started.")
        return True

    def do(self, interval: float = 5):
        # 1、Start collect log.
        if not self.start_log_thread():
            return

        # 2、Check whether the stop file for detecting exit exists.
        stop_file = f"stop_log_{g_name_space}"
        threading.Thread(target=self.monitor_stop, args=(stop_file,), name="thread-monitor", daemon=True).start()

        # 3、Start collect log loop with idle timeout.
        idle_since = time.monotonic() if self._idle else None

        while not self.exit_flag.is_set():
            if not self.start_log_thread():
                break

            if self._idle:
                if idle_since is None:
                    idle_since = time.monotonic()
                    log_w(f"No pods in namespace '{g_name_space}'. Will exit after {IDLE_EXIT_SECONDS}s idle.")
                elif time.monotonic() - idle_since >= IDLE_EXIT_SECONDS:
                    log_i(f"No pods in namespace for {IDLE_EXIT_SECONDS}s, exiting gracefully.")
                    self.exit_flag.set()
                    break
            else:
                idle_since = None

            time.sleep(interval)

        # 4、Exit gracefully.
        for thread in self.threads:
            thread.join()
        log_i("All tasks have been terminated")


def read_config(config_file: str) -> None:
    """
    Read and apply the contents of the configuration file.
    :param config_file: Path to the configuration file
    """
    global g_name_space, g_target_log, g_max_log_size, g_backup_count

    script_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.normpath(config_file if os.path.isabs(config_file) else os.path.join(script_dir, config_file))

    config = configparser.ConfigParser()
    if not config.read(cfg_path):
        log_e(f"The configuration file {cfg_path} does not exist or cannot be read.")
        sys.exit(1)

    log_section = "LogSetting"

    for sec_name in config.sections():
        log_i(f"[{sec_name}]")
        for key, value in config[sec_name].items():
            log_i(f"{key} = {value}")

    if log_section not in config:
        log_e(
            f"The [{log_section}] section is missing in {cfg_path}; "
            "add it and required keys (see the template in the same directory)."
        )
        sys.exit(1)

    try:
        g_max_log_size = config[log_section].getint("max_log_size", g_max_log_size)
        g_backup_count = config[log_section].getint("backup_count", g_backup_count)
    except ValueError as e:
        log_e(f"Invalid integer in [{log_section}] (max_log_size or backup_count): {e}")
        sys.exit(1)

    g_name_space = (config[log_section].get("name_space", g_name_space) or "").strip()
    out_raw = (config[log_section].get("out_path", g_target_log) or "").strip() or g_target_log
    if os.path.isabs(out_raw):
        out_base = os.path.normpath(out_raw)
    else:
        out_base = os.path.normpath(os.path.join(script_dir, out_raw))

    if g_max_log_size <= 0:
        log_e(f"max_log_size must be positive, got {g_max_log_size}.")
        sys.exit(1)
    if g_backup_count < 0:
        log_e(f"backup_count must be non-negative, got {g_backup_count}.")
        sys.exit(1)

    session_dir = os.path.join(out_base, datetime.now(timezone.utc).astimezone().strftime('%Y%m%d_%H%M%S'))
    g_target_log = os.path.abspath(session_dir)
    log_i(f"Read configuration: [{log_section}] succeeded. Pod logs directory (absolute): {g_target_log}")


def _set_process_title(namespace: str) -> None:
    """Set the process title to ``log_monitor:<namespace>`` so ``delete.sh``
    can locate and kill the collector by namespace.  Falls back to a PID file
    if the ``setproctitle`` package is not installed.
    """
    title = f"log_monitor:{namespace}"
    try:
        import setproctitle  # type: ignore[import-untyped]

        setproctitle.setproctitle(title)
        return
    except ImportError:
        pass
    # Fallback: write PID file for delete.sh
    pid_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f".log_monitor_{namespace}.pid",
    )
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


if __name__ == "__main__":
    read_config("log_config.ini")
    _set_process_title(g_name_space)
    log_i(f"Use the command [touch {os.getcwd()}/stop_log_{g_name_space}] to stop the background process.")
    LogMonitor().do()
