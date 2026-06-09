# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import re
import time
import subprocess
from tqdm import tqdm
import threading
import sys


ENCODE_TYPE = "utf-8"
CMD_KUBECTL = "/usr/bin/kubectl"
CMD_AWK = "/usr/bin/awk"
CMD_GREP = "/usr/bin/grep"

PROGRESS_TOTAL = 100
SAFETENSORS_WEIGHT = 2
SLEEP_POLL_INTERVAL = 0.1
SLEEP_LINE_INTERVAL = 0.05
PROCESS_TERMINATE_TIMEOUT = 5
SEPARATOR_WIDTH = 80


class VLLMProgressMonitor:
    """
    Monitor vLLM pod startup progress by parsing log output.

    This class monitors multiple vLLM pods simultaneously by following their
    kubectl logs and identifying key startup steps to update progress bars.
    """

    key_steps = {
        'NodeManagerAPI server is ready': 10,
        'engine_server --dp-rank': 20,
        'Loading safetensors': 30,
        'Loading model weights': 80,
        'Graph capturing finished': 90,
        'EndpointStatus.INITIAL to EndpointStatus.NORMAL': 100,
    }

    def __init__(self):
        """
        Initialize the progress monitor.

        Attributes:
            data: Dictionary mapping pod names to their progress bars.
            completed: Dictionary mapping pod names to completion status.
            lock: Thread lock for synchronizing progress bar updates.
        """
        self.data: dict[str, tqdm] = {}
        self.completed: dict[str, bool] = {}
        self.lock = threading.Lock()

    def parse_log_line(self, line: str) -> int:
        """
        Parse a log line to identify vLLM startup progress.

        Analyzes the given log line to detect key startup steps defined in
        key_steps and returns the corresponding progress percentage.

        Args:
            line: A single line of log output from vLLM pod.

        Returns:
            Progress percentage (0-100) if a key step is matched,
            otherwise 0. For 'Loading safetensors', returns a weighted
            progress based on the percentage found in the log line.
        """
        for step_name, step_value in self.key_steps.items():
            if step_name not in line:
                continue
            if step_name == 'Loading safetensors':
                percent_match = re.search(r'(\d+)%', line)
                if percent_match:
                    return int(percent_match.group(1)) / SAFETENSORS_WEIGHT + step_value
            return step_value

        return 0

    def update_progress(self, step: int, pod_name: str) -> None:
        """
        Update the progress bar for a specific pod.

        Calculates the incremental progress and updates the tqdm progress bar
        associated with the given pod.

        Args:
            step: The new progress value (percentage, typically 0-100).
            index: The log line index (currently unused, reserved for future).
            pod_name: The name of the pod whose progress bar to update.
        Returns:
            None
        """
        if step <= 0:
            return

        progress_bar = self.data.get(pod_name)
        if progress_bar:
            with self.lock:
                progress_bar.update(step - progress_bar.n)

    def update_description(self, pod_name: str, line_index: int, error_cnt: int, is_error: bool) -> None:
        progress_bar = self.data.get(pod_name)
        if not progress_bar:
            return
        msg = "[ERROR]" if is_error else "[_____]"
        with self.lock:
            progress_bar.set_description(f"[{pod_name}], line:[{line_index}], err:[{error_cnt}]{msg}")

    def shell_pull_log(self, name_space: str, pod_name: str) -> None:
        """
        Continuously pull and parse logs from a Kubernetes pod.

        Executes kubectl logs command to stream logs from the specified pod,
        parses each line to identify startup progress, and updates the
        progress bar accordingly. Runs until startup completes or an error occurs.

        Args:
            name_space: The Kubernetes namespace where the pod is located.
            pod_name: The name of the pod to monitor.

        Returns:
            None
        """
        progress_bar = tqdm(
            total=PROGRESS_TOTAL,
            desc="vLLM start step",
            unit="%",
            bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]',
        )
        self.data[pod_name] = progress_bar

        try:
            with subprocess.Popen(
                [CMD_KUBECTL, 'logs', '-f', '-n', name_space, pod_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            ) as process:
                line_index = 0
                error_cnt = 0
                while True:
                    line_index += 1
                    line = process.stdout.readline()

                    if not line:
                        if process.poll() is not None:
                            self.update_description(pod_name, line_index, error_cnt, True)
                            break
                        time.sleep(SLEEP_POLL_INTERVAL)
                        continue

                    log_line = line.rstrip('\n')
                    if log_line:
                        if "ERROR" in log_line:
                            error_cnt += 1
                        self.update_description(pod_name, line_index, error_cnt, False)

                        step = self.parse_log_line(log_line)
                        if step > 0:
                            self.update_progress(step, pod_name)

                        if step == PROGRESS_TOTAL:
                            self.completed[pod_name] = True
                            break
                    time.sleep(SLEEP_LINE_INTERVAL)
                process.terminate()
                process.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
        except Exception as e:
            print(f"{pod_name} :Exception: {e}")

    def start(self, list_pod: list[str], name_space: str) -> None:
        """
        Start monitoring multiple vLLM pods simultaneously.

        Creates a thread for each pod to monitor logs in parallel,
        then waits for all threads to complete.

        Args:
            list_pod: List of pod names to monitor.
            name_space: The Kubernetes namespace where the pods are located.

        Returns:
            None
        """
        print("")
        print("━" * SEPARATOR_WIDTH)

        thread_list = []
        for pod_name in list_pod:
            thread = threading.Thread(
                target=self.shell_pull_log,
                args=(
                    name_space,
                    pod_name,
                ),
                name=f"t-{pod_name}",
                daemon=True,
            )
            thread_list.append(thread)
            thread.start()

        for thread in thread_list:
            thread.join()

        for progress_bar in self.data.values():
            if progress_bar:
                progress_bar.close()

        print("━" * SEPARATOR_WIDTH)


def shell_get_pod(name_space: str) -> list[str] | None:
    """
    Get list of running vLLM pods in a namespace.

    Executes kubectl command with awk and grep filters to retrieve
    only Running pods with names starting with 'vllm-'.

    Args:
        name_space: The Kubernetes namespace to query for pods.

    Returns:
        A list of pod names that are in Running state and start with 'vllm-',
        or None if an error occurs during command execution.
    """
    try:
        with (
            subprocess.Popen([CMD_KUBECTL, 'get', 'pods', '-n', name_space], stdout=subprocess.PIPE) as kubectl_cmd,
            subprocess.Popen(
                [CMD_AWK, 'NR>1 && $3=="Running" {print $1}'],
                stdin=kubectl_cmd.stdout,
                stdout=subprocess.PIPE,
            ) as awk_cmd,
            subprocess.Popen([CMD_GREP, 'vllm-'], stdin=awk_cmd.stdout, stdout=subprocess.PIPE) as grep_cmd,
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
    """
    Update the log display with current pod waiting status.

    Clears the previous log line and writes the updated waiting status
    to the console without moving to a new line.

    Args:
        len_list_pod: The current number of running pods found.
        pod_num: The expected total number of pods to wait for.

    Returns:
        None
    """
    sys.stdout.write('\033[2K\r')
    sys.stdout.write(f"  Waiting for pod running: [{len_list_pod}/{pod_num}]")
    sys.stdout.flush()


def start_monitor(name_space: str, pod_num: int) -> None:
    """
    Wait for pods to be ready and start monitoring their progress.

    Continuously checks for running vLLM pods until the expected number
    is reached, then initiates progress monitoring for all discovered pods.

    Args:
        name_space: The Kubernetes namespace to monitor.
        pod_num: The expected number of pods to wait for before starting monitoring.

    Returns:
        None
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
        sys.exit(1)

    monitor = VLLMProgressMonitor()
    monitor.start(list_pod, name_space)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("namespace", type=str, help="Namespace name to monitor")
    parser.add_argument("pod_cnt", type=int, help="Count of pod")
    args = parser.parse_args()
    start_monitor(args.namespace, args.pod_cnt)
