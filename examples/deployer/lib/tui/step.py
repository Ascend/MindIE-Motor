# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""vLLM pod startup progress monitoring.

Supports two output modes:

- **TUI mode** — writes progress into a :class:`PodProgressState` for
  rendering inside :class:`DeployInteractiveSession`.
- **tqdm mode** — when ``progress_state`` is ``None`` and ``use_tqdm`` is
  ``True``, creates classic tqdm progress bars for the non-TUI CLI deploy
  path.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time

from tqdm import tqdm


def _detect_cmd(name: str, fallback: str) -> str:
    """Return the absolute path of *name*, falling back if not on PATH."""
    found = shutil.which(name)
    return found if found else fallback


ENCODE_TYPE = "utf-8"
CMD_KUBECTL = _detect_cmd("kubectl", "/usr/bin/kubectl")
CMD_AWK = _detect_cmd("awk", "/usr/bin/awk")
CMD_GREP = _detect_cmd("grep", "/usr/bin/grep")

PROGRESS_TOTAL = 100
SAFETENSORS_WEIGHT = 2
SLEEP_POLL_INTERVAL = 0.1
DESCRIPTION_UPDATE_INTERVAL = 1.0
SAFETENSORS_UPDATE_INTERVAL = 0.3
PROCESS_TERMINATE_TIMEOUT = 5
SEPARATOR_WIDTH = 80

KEY_STEPS = {
    'NodeManagerAPI server is ready': 10,
    'engine_server --dp-rank': 20,
    'Loading safetensors': 30,
    'Loading model weights': 80,
    'Graph capturing finished': 90,
    'EndpointStatus.INITIAL to EndpointStatus.NORMAL': 100,
}


def parse_log_line(line: str) -> int:
    """Parse a log line to identify vLLM startup progress.

    Returns:
        Progress percentage (0-100) if a key step is matched,
        otherwise 0.  For ``Loading safetensors``, returns a weighted
        progress based on the percentage found in the log line.
    """
    for step_name, step_value in KEY_STEPS.items():
        if step_name not in line:
            continue
        if step_name == 'Loading safetensors':
            percent_match = re.search(r'(\d+)%', line)
            if percent_match:
                return int(int(percent_match.group(1)) / SAFETENSORS_WEIGHT + step_value)
        return step_value

    return 0


class VLLMProgressMonitor:
    """Monitor vLLM pod startup progress by parsing log output.

    Two output modes:
        - **TUI**: pass ``progress_state`` — progress is written into the
          shared :class:`PodProgressState` for the TUI rendering loop.
        - **tqdm**: set ``use_tqdm=True`` (and leave ``progress_state`` as
          ``None``) — classic tqdm progress bars are displayed directly
          on the terminal.
    """

    key_steps = KEY_STEPS
    _key_step_markers = tuple(KEY_STEPS.keys())

    def __init__(self, cancel_event=None, progress_state=None, *, use_tqdm: bool = False):
        self.completed: dict[str, bool] = {}
        self.lock = threading.Lock()
        self.cancel_event = cancel_event or threading.Event()
        self._processes: dict[str, subprocess.Popen] = {}
        self._progress_state = progress_state
        self._use_tqdm = use_tqdm and (progress_state is None)
        # tqdm mode state
        self._tqdm_bars: dict[str, tqdm] = {}

    # ------------------------------------------------------------------
    # Log parsing (shared between both modes)
    # ------------------------------------------------------------------

    @classmethod
    def is_relevant_log_line(cls, line: str) -> bool:
        return any(marker in line for marker in cls._key_step_markers)

    def parse_log_line(self, line: str) -> int:
        return parse_log_line(line)

    # ------------------------------------------------------------------
    # Progress updates
    # ------------------------------------------------------------------

    def update_progress(self, step: int, pod_name: str) -> None:
        """Update progress for *pod_name*.  Dispatches to tqdm or PodProgressState."""
        if step <= 0:
            return
        if self._use_tqdm:
            pbar = self._tqdm_bars.get(pod_name)
            if pbar and step > pbar.n:
                with self.lock:
                    pbar.update(step - pbar.n)
        elif self._progress_state:
            self._progress_state.update(pod_name, step)

    def update_description(self, pod_name: str, line_index: int, error_cnt: int, is_error: bool) -> None:
        """Update the per-pod description / line counter."""
        if self._use_tqdm:
            pbar = self._tqdm_bars.get(pod_name)
            if pbar:
                msg = "[ERROR]" if is_error else "[_____]"
                with self.lock:
                    pbar.set_description(f"[{pod_name}], line:[{line_index}], err:[{error_cnt}]{msg}")
        elif self._progress_state:
            self._progress_state.increment_line(pod_name)

    def update_description_throttled(
        self, pod_name: str, line_index: int, error_cnt: int, last_update_at: float, *, force: bool = False
    ) -> float:
        """Update description at most once per ``DESCRIPTION_UPDATE_INTERVAL``."""
        now = time.monotonic()
        if force or now - last_update_at >= DESCRIPTION_UPDATE_INTERVAL:
            self.update_description(pod_name, line_index, error_cnt, is_error=False)
            return now
        return last_update_at

    def should_update_safetensors_progress(self, log_line: str, step: int, last_update_at: float) -> bool:
        """Throttle safetensors percentage updates to avoid overhead."""
        if step >= PROGRESS_TOTAL or 'Loading safetensors' not in log_line:
            return True
        return time.monotonic() - last_update_at >= SAFETENSORS_UPDATE_INTERVAL

    # ------------------------------------------------------------------
    # Per-pod log tailing
    # ------------------------------------------------------------------

    def shell_pull_log(self, name_space: str, pod_name: str) -> None:
        """Tail ``kubectl logs -f`` for *pod_name* and update progress.

        In TUI mode registers the pod with ``_progress_state`` and writes
        incremental progress.  In tqdm mode creates a tqdm progress bar.
        """
        if self._use_tqdm:
            pbar = tqdm(
                total=PROGRESS_TOTAL,
                desc="vLLM start step",
                unit="%",
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]',
            )
            self._tqdm_bars[pod_name] = pbar
        elif self._progress_state:
            self._progress_state.register(pod_name)

        try:
            with subprocess.Popen(
                [CMD_KUBECTL, 'logs', '-f', '-n', name_space, pod_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            ) as process:
                self._processes[pod_name] = process
                line_index = 0
                error_cnt = 0
                last_desc_update = 0.0
                last_safe_update = 0.0
                while not self.cancel_event.is_set():
                    line = process.stdout.readline()
                    if not line:
                        line_index += 1
                        if process.poll() is not None:
                            self.update_description(pod_name, line_index, error_cnt, is_error=True)
                            break
                        time.sleep(SLEEP_POLL_INTERVAL)
                        continue
                    line_index += 1
                    log_line = line.rstrip('\n')
                    if not log_line:
                        continue
                    if "ERROR" in log_line:
                        error_cnt += 1
                        if self._progress_state:
                            self._progress_state.add_error(pod_name, log_line, line_index)
                    last_desc_update = self.update_description_throttled(
                        pod_name, line_index, error_cnt, last_desc_update
                    )
                    if not self.is_relevant_log_line(log_line):
                        continue
                    step = self.parse_log_line(log_line)
                    if step <= 0:
                        continue
                    if self.should_update_safetensors_progress(log_line, step, last_safe_update):
                        self.update_progress(step, pod_name)
                        if 'Loading safetensors' in log_line:
                            last_safe_update = time.monotonic()
                    if step == PROGRESS_TOTAL:
                        self.completed[pod_name] = True
                        break
                process.terminate()
                process.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
        except Exception as e:
            print(f"{pod_name} :Exception: {e}")
        finally:
            if not self._use_tqdm:
                self._processes.pop(pod_name, None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Signal all monitoring threads to stop."""
        self.cancel_event.set()
        for _pod_name, proc in list(self._processes.items()):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except (ProcessLookupError, OSError):
                    pass

    def start(self, list_pod: list[str], name_space: str) -> None:
        """Start monitoring threads for *list_pod* and wait for completion.

        In tqdm mode also prints a separator header and closes all bars on
        exit.
        """
        if self._use_tqdm:
            print("")
            print("━" * SEPARATOR_WIDTH)

        thread_list = []
        for pod_name in list_pod:
            thread = threading.Thread(
                target=self.shell_pull_log,
                args=(name_space, pod_name),
                name=f"t-{pod_name}",
                daemon=True,
            )
            thread_list.append(thread)
            thread.start()

        for thread in thread_list:
            while thread.is_alive():
                thread.join(timeout=0.5)
                if self.cancel_event.is_set():
                    self.cancel()
                    break

        if self._use_tqdm:
            for pbar in self._tqdm_bars.values():
                pbar.close()
            self._tqdm_bars.clear()
            print("━" * SEPARATOR_WIDTH)


# ------------------------------------------------------------------
# Pod discovery helpers
# ------------------------------------------------------------------


def shell_get_pod(name_space: str):
    """Get list of Running vLLM engine pods in *name_space*.

    Returns:
        List of pod names, or ``None`` on error.
    """
    try:
        with (
            subprocess.Popen(
                [CMD_KUBECTL, 'get', 'pods', '-n', name_space],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as kubectl_cmd,
            subprocess.Popen(
                [CMD_AWK, 'NR>1 && $3=="Running" {print $1}'],
                stdin=kubectl_cmd.stdout,
                stdout=subprocess.PIPE,
            ) as awk_cmd,
            subprocess.Popen(
                [CMD_GREP, 'vllm-'],
                stdin=awk_cmd.stdout,
                stdout=subprocess.PIPE,
            ) as grep_cmd,
            subprocess.Popen(
                [CMD_GREP, '-v', '-e', '-controller-', '-e', '-coordinator-', '-e', '-kv-'],
                stdin=grep_cmd.stdout,
                stdout=subprocess.PIPE,
            ) as grep_v_cmd,
        ):
            output, _ = grep_v_cmd.communicate()
            return output.decode(ENCODE_TYPE).strip().splitlines()
    except Exception as e:
        print(f"shell_get_pod Exception: {e}")
        return None


def update_log_display(len_list_pod: int, pod_num: int) -> None:
    """Update the in-place log display with current pod waiting status."""
    sys.stdout.write('\033[2K\r')
    sys.stdout.write(f"  Waiting for pod running: [{len_list_pod}/{pod_num}]")
    sys.stdout.flush()


# ------------------------------------------------------------------
# Non-TUI entry point (tqdm mode)
# ------------------------------------------------------------------


def start_monitor(name_space: str, pod_num: int) -> None:
    """Wait for pods to be ready, then start tqdm-based progress monitoring.

    Continuously checks for Running vLLM pods until the expected count is
    reached, then uses :class:`VLLMProgressMonitor` in tqdm mode to display
    per-pod startup progress.
    """
    print("━" * SEPARATOR_WIDTH)
    while True:
        time.sleep(1)
        list_pod = shell_get_pod(name_space)
        if list_pod is None:
            continue
        update_log_display(len(list_pod), pod_num)
        if len(list_pod) >= pod_num:
            break

    if not list_pod:
        print("No running vLLM pods found")
        return

    monitor = VLLMProgressMonitor(use_tqdm=True)
    monitor.start(list_pod, name_space)
