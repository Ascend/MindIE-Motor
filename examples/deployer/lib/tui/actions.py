# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Action mixin for DeployInteractiveSession.

Contains all action methods: deploy, update, delete, log collection,
progress monitoring, and text input.
"""

from __future__ import annotations

import configparser
import io
import os
import shutil
import subprocess
import threading
import time

from lib import constant as C
from .state import PodProgressState
from .tui_utils import visible_length


class _DeployActionsMixin:  # pylint: disable=no-member,attribute-defined-outside-init
    """Mixin providing action methods for :class:`DeployInteractiveSession`.

    Expects the following attributes on ``self``:
        - ``_ctx``, ``_running``, ``_deployed``, ``_log_running``
        - ``_progress_active``, ``_pod_progress``, ``_cancel_event``
        - ``_monitor_threads``, ``_menu_selected``
        - ``_status_msg``, ``_confirm_action``, ``_confirm_expiry``
        - ``_deploy_log_lines``
        - ``namespace``, ``pod_cnt``, ``user_config``, ``deployer_dir``, ``width``

    And the following methods on ``self``:
        - ``_set_status()``, ``_set_confirm()``, ``_clear_confirm()``
        - ``_flush_status()``, ``_cleanup_monitors()``
    """

    # ------------------------------------------------------------------
    # Pod re-scan (picks up pods that appear after initial discovery)
    # ------------------------------------------------------------------

    @staticmethod
    def _pod_prefix(pod_name: str) -> str:
        """Return the role prefix of a Kubernetes pod name.

        Pod names are ``{name}-{replicaset_hash}-{pod_hash}``; the prefix
        (everything before the last two dashes) identifies the role across
        restarts.  e.g. ``vllm-p0-abc123-def456`` → ``vllm-p0``.
        """
        parts = pod_name.rsplit("-", 2)
        return parts[0] if len(parts) == 3 else pod_name

    def _rescan_pods(self) -> None:
        """Check for newly-appeared pods and start monitoring them.

        Called periodically from the main loop while progress is active,
        so pods that become Ready after initial discovery are picked up.
        Also detects pod restarts (same role prefix, different name) and
        replaces the stale entry.
        """
        from .step import shell_get_pod  # pylint: disable=import-outside-toplevel

        new_pods = shell_get_pod(self.namespace)
        if not new_pods:
            return
        current = self._pod_progress.get_all()
        current_set = set(current.keys())

        new_pods_set = set(new_pods)

        # Start monitoring for genuinely new pods (not yet tracked)
        added = 0
        for pod_name in new_pods:
            if pod_name not in current_set:
                self._pod_progress.register(pod_name)
                t = threading.Thread(
                    target=self._progress_monitor.shell_pull_log,
                    args=(self.namespace, pod_name),
                    daemon=True,
                )
                self._monitor_threads.append(t)
                t.start()
                added += 1
        if added:
            self._set_status(
                f"{C.Style.GREEN}+{added} new pod(s) discovered{C.Style.RESET}",
                2.0,
            )

        # Detect real pod restarts: old pod no longer exists, but a new pod
        # with the same role prefix has appeared in its place
        new_by_prefix: dict[str, list[str]] = {}
        for p in new_pods:
            prefix = self._pod_prefix(p)
            new_by_prefix.setdefault(prefix, []).append(p)

        for old_name in list(current_set):
            if old_name in new_pods_set:
                continue  # still alive, no restart
            old_prefix = self._pod_prefix(old_name)
            candidates = new_by_prefix.get(old_prefix, [])
            # Only flag as restarted if there's exactly one new pod for this
            # prefix and the old one is gone (not just a multi-replica false positive)
            if len(candidates) == 1:
                self._pod_progress.remove(old_name)
                current_set.discard(old_name)
                self._set_status(
                    f"{C.Style.YELLOW}Pod restarted: {old_name} → {candidates[0]}{C.Style.RESET}",
                    2.0,
                )

    # ------------------------------------------------------------------
    # Pod status watcher ("kubectl get pods" overview)
    # ------------------------------------------------------------------

    def _refresh_pod_status(self) -> None:
        """Run ``kubectl get pods -o wide`` and update the shared status dict."""
        from .step import CMD_KUBECTL  # pylint: disable=import-outside-toplevel

        if not self.namespace:
            return
        try:
            proc = subprocess.run(
                [
                    CMD_KUBECTL,
                    "get",
                    "pods",
                    "-n",
                    self.namespace,
                    "--no-headers",
                    "-o",
                    "wide",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0:
                stderr_text = proc.stderr.strip()[:200]
                with self._pod_status_lock:
                    self._pod_status = {"⚠ kubectl error": stderr_text or f"rc={proc.returncode}"}
                return
        except (subprocess.TimeoutExpired, OSError) as e:
            with self._pod_status_lock:
                self._pod_status = {"⚠ kubectl error": str(e)[:200]}
            return

        # -o wide columns: NAME READY STATUS RESTARTS AGE IP NODE ...
        status: dict[str, str] = {}
        for line in proc.stdout.strip().split("\n"):
            if not line.strip():
                continue
            cols = line.split()
            if len(cols) < 7:
                continue
            name = cols[0]
            ready = cols[1]
            phase = cols[2]
            restarts = cols[3]
            age = cols[4]
            pod_ip = cols[5] if cols[5] != "<none>" else ""
            node = cols[6] if cols[6] != "<none>" else ""
            status[name] = f"{ready:>5s}  {phase:<10s}  {restarts:>8s}  {age:>5s}  {pod_ip:<15s}  {node}"
        with self._pod_status_lock:
            self._pod_status = status if status else {"(no pods)": ""}

    def _run_pod_watcher(self) -> None:
        """Background thread target: refresh pod status periodically."""
        while not self._pod_watcher_stop.is_set():
            self._refresh_pod_status()
            self._pod_watcher_stop.wait(timeout=3)

    def _start_pod_watcher(self) -> None:
        """Start the pod-status background watcher."""
        self._pod_watcher_stop.clear()
        self._pod_watcher_thread = threading.Thread(target=self._run_pod_watcher, daemon=True)
        self._pod_watcher_thread.start()

    def _stop_pod_watcher(self) -> None:
        """Stop the pod-status background watcher."""
        self._pod_watcher_stop.set()
        if self._pod_watcher_thread and self._pod_watcher_thread.is_alive():
            self._pod_watcher_thread.join(timeout=2)
        self._pod_watcher_thread = None

    # ------------------------------------------------------------------
    # Progress toggle (inline in main menu)
    # ------------------------------------------------------------------

    def _toggle_progress(self) -> None:
        """Toggle inline progress bars on / off."""
        if self._progress_active:
            self._stop_progress_monitor()
        else:
            self._start_progress_monitor()

    def _start_progress_monitor(self) -> None:
        """Discover pods and start per-pod log monitor threads."""
        from .step import VLLMProgressMonitor, shell_get_pod  # pylint: disable=import-outside-toplevel

        if self._ctx is None:
            return

        # Wipe stale state from any previous run.
        # _cleanup_monitors SETS the cancel event to stop old threads;
        # clear it AFTER so new threads start with a fresh event.
        self._cleanup_monitors()
        self._cancel_event.clear()

        # Wait briefly for pods
        list_pod = None
        self._set_status(
            f"{C.Style.DIM}Scanning for pods in {self.namespace}...{C.Style.RESET}",
            duration=5,
        )
        deadline = time.monotonic() + C.POD_WAIT_INTERVAL * 2
        while self._running and time.monotonic() < deadline:
            list_pod = shell_get_pod(self.namespace)
            if list_pod and len(list_pod) >= self.pod_cnt:
                break
            time.sleep(0.5)

        if not list_pod:
            self._set_status(
                f"{C.Style.RED}✗ No pods found in {self.namespace}.{C.Style.RESET}",
                2.0,
            )
            return

        self._pod_progress = PodProgressState()
        # Use VLLMProgressMonitor as the log-tailing engine; it writes
        # directly into _pod_progress (shared state).
        self._progress_monitor = VLLMProgressMonitor(cancel_event=self._cancel_event, progress_state=self._pod_progress)
        for pod_name in list_pod:
            t = threading.Thread(
                target=self._progress_monitor.shell_pull_log,
                args=(self.namespace, pod_name),
                daemon=True,
            )
            self._monitor_threads.append(t)
            t.start()

        self._progress_active = True
        self._set_status(
            f"{C.Style.BOLD}{C.Style.GREEN}✓{C.Style.RESET}  "
            f"{C.Style.GREEN}Monitoring {len(list_pod)} pod(s)...{C.Style.RESET}",
            2.0,
        )

    def _stop_progress_monitor(self) -> None:
        """Cancel all monitor threads and hide progress bars."""
        self._progress_active = False
        if hasattr(self, '_progress_monitor'):
            self._progress_monitor.cancel()
        self._cleanup_monitors()
        self._set_status(
            f"{C.Style.YELLOW}Progress closed.{C.Style.RESET}",
            1.5,
        )

    # ------------------------------------------------------------------
    # Log collection toggle
    # ------------------------------------------------------------------

    def _handle_log_key(self) -> None:
        """Handle [L] key — starts immediately, confirms when already running."""
        if self._log_running:
            if self._confirm_action == 'log_restart':
                self._clear_confirm()
                self._stop_log_collection()
                self._start_log_collection()
                self._set_status(
                    f"{C.Style.BOLD}{C.Style.CYAN}[L]{C.Style.RESET} → "
                    f"{C.Style.GREEN}Log collection restarted ✓{C.Style.RESET}",
                    2.0,
                )
            elif self._confirm_action == 'log_stop':
                self._clear_confirm()
                self._stop_log_collection()
                self._set_status(
                    f"{C.Style.BOLD}{C.Style.CYAN}[L]{C.Style.RESET} → "
                    f"{C.Style.YELLOW}Log collection stopped.{C.Style.RESET}",
                    2.0,
                )
            else:
                self._set_confirm('log_restart')
                self._flush_status()
        else:
            if self._confirm_action:
                self._clear_confirm()
            self._start_log_collection()
            self._set_status(
                f"{C.Style.BOLD}{C.Style.CYAN}[L]{C.Style.RESET} → "
                f"{C.Style.GREEN}Log collection started ✓{C.Style.RESET}",
                2.0,
            )

    def _start_log_collection(self) -> None:
        deploy_config = self.user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
        job_id = deploy_config.get(C.CONFIG_JOB_ID, self.namespace)
        if not job_id:
            return

        ini_path = os.path.join(self.deployer_dir, "log_collect", "log_config.ini")
        if not os.path.exists(ini_path):
            self._set_status(f"{C.Style.RED}✗ log_config.ini not found{C.Style.RESET}", 2.0)
            return

        config = configparser.ConfigParser()
        try:
            config.read(ini_path)
            config.set("LogSetting", "name_space", job_id)
        except configparser.Error as e:
            self._set_status(f"{C.Style.RED}✗ Invalid log_config.ini: {e}{C.Style.RESET}", 3.0)
            return
        # Atomic write: temp file then rename to avoid corrupting ini on failure
        tmp_path = ini_path + ".tmp"
        with open(tmp_path, "w", encoding='utf-8') as f:
            config.write(f)
        os.replace(tmp_path, ini_path)

        show_log_path = os.path.join(self.deployer_dir, "show_log.sh")

        def _run():
            try:
                proc = subprocess.run(
                    ["/bin/bash", show_log_path],
                    cwd=self.deployer_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except (FileNotFoundError, OSError) as e:
                self._log_running = False
                self._set_status(
                    f"{C.Style.RED}✗ Log collection failed: {e}{C.Style.RESET}",
                    4.0,
                )
                return
            stdout = proc.stdout.strip()
            success = proc.returncode == 0 or ("logs save to" in stdout)
            self._log_running = success
            if stdout:
                self._deploy_log_lines = stdout.split("\n")
            if not success:
                self._set_status(
                    f"{C.Style.RED}✗ Log collection failed (rc={proc.returncode}){C.Style.RESET}",
                    4.0,
                )

        threading.Thread(target=_run, daemon=True).start()
        self._log_running = True  # optimistic; _run sets to False on real failure

    def _stop_log_collection(self) -> None:
        stop_file = os.path.join(self.deployer_dir, "log_collect", f"stop_log_{self.namespace}")
        try:
            with open(stop_file, 'w', encoding='utf-8') as f:
                f.write('stop')
            time.sleep(1.5)
            if os.path.exists(stop_file):
                os.remove(stop_file)
        except OSError as e:
            self._set_status(
                f"{C.Style.RED}✗ Failed to stop log collection: {e}{C.Style.RESET}",
                3.0,
            )
        finally:
            self._log_running = False

    # ------------------------------------------------------------------
    # Text input (raw terminal mode)
    # ------------------------------------------------------------------

    def _read_line(self, prompt: str, default: str = "") -> str | None:
        """Read a line of text from the user.  Returns ``None`` on Escape.

        Supports Tab path completion with bash-like behaviour:
        first Tab completes the common prefix; a second Tab (without
        the buffer changing) displays all candidates below the input.
        """
        ctx = self._ctx
        if ctx is None:
            return None

        buffer = list(default)
        input_row = getattr(self, '_last_box_end_row', 10) + 1
        _tab_matches: list[str] = []  # current completion candidates

        def _clear_candidates():
            nonlocal _tab_matches
            _tab_matches = []
            # Wipe candidate display rows
            for i in range(10):
                ctx.write_at(input_row + 1 + i, 1, '\033[K')

        def _show_candidates(matches: list[str]):
            nonlocal _tab_matches
            _tab_matches = matches
            # Clear old candidates first
            for i in range(10):
                ctx.write_at(input_row + 1 + i, 1, '\033[K')
            names = [os.path.basename(m) + ('/' if os.path.isdir(m) else '') for m in sorted(matches)]
            for i, name in enumerate(names[:8]):
                ctx.write_at(
                    input_row + 1 + i,
                    1,
                    f"    {C.Style.DIM}{name}{C.Style.RESET}\033[K",
                )
            if len(names) > 8:
                ctx.write_at(
                    input_row + 9,
                    1,
                    f"    {C.Style.DIM}... and {len(names) - 8} more{C.Style.RESET}\033[K",
                )

        def _redraw():
            displayed = ''.join(buffer)
            cursor = '_' if not displayed else ''
            ctx.write_at(
                input_row,
                1,
                f"  {prompt} {C.Style.BOLD}{C.Style.CYAN}{displayed}{cursor}{C.Style.RESET}\033[K",
            )
            ctx.move_to(input_row, 3 + visible_length(prompt) + len(displayed))

        _redraw()

        while self._running:
            key = ctx.poll_key(timeout=0.1)
            if key is None:
                continue

            code = ord(key)

            if code == 0x0D:  # Enter
                _clear_candidates()
                result = ''.join(buffer).strip()
                ctx.write_at(input_row, 1, '\033[K')
                return result if result else None
            if code == 0x1B:  # Escape
                _clear_candidates()
                ctx.write_at(input_row, 1, '\033[K')
                return None
            if code == 0x03:  # Ctrl-C
                _clear_candidates()
                ctx.write_at(input_row, 1, '\033[K')
                return None
            if code in (0x7F, 0x08):  # Backspace
                if buffer:
                    buffer.pop()
                _redraw()
            elif code == 0x09:  # Tab — path completion
                completed, matches = self._complete_path(buffer)
                if completed:
                    _redraw()
                if len(matches) == 1:
                    _clear_candidates()
                    _tab_matches = []
                elif len(matches) > 1:
                    _tab_matches = matches
                    _show_candidates(matches)
                else:
                    _clear_candidates()
                    _tab_matches = []
            elif 0x20 <= code <= 0x7E:  # Printable ASCII
                buffer.append(key)
                _redraw()

        return None

    @staticmethod
    def _complete_path(buffer: list[str]) -> tuple[bool, list[str]]:
        """Tab-complete the last path segment in *buffer* (mutates in place).

        Returns (completed: bool, matches: list[str]).
        """
        text = ''.join(buffer)
        if text.endswith('/'):
            partial = ''
            base = text
        else:
            base, _, partial = text.rpartition('/')
            if not base:
                base = '.'
        pattern = os.path.join(base, partial + '*')
        import glob

        matches = sorted(glob.glob(pattern))
        if not matches:
            return (False, [])
        common = os.path.commonprefix(matches)
        suffix = common[len(os.path.join(base, partial)) :]
        completed = bool(suffix)
        if len(matches) == 1:
            if os.path.isdir(matches[0]) and not suffix.endswith('/'):
                suffix += '/'
            completed = True
        if suffix:
            buffer.clear()
            buffer.extend(text + suffix)
        return (completed, matches)

    # ------------------------------------------------------------------
    # Deploy / Update config from within TUI
    # ------------------------------------------------------------------

    def _deploy_service(self) -> None:
        """Prompt for a config directory, then deploy services."""
        ctx = self._ctx
        if ctx is None:
            return

        ctx.flush_input()
        self._status_msg = None

        self._set_status(
            f"{C.Style.BOLD}{C.Style.GREEN}[R]{C.Style.RESET} → "
            f"{C.Style.BOLD}Deploy services{C.Style.RESET}  —  "
            f"{C.Style.DIM}Enter config directory path, Enter to confirm, Esc to cancel{C.Style.RESET}",
            duration=60,
        )

        config_dir = self._read_line(
            f"{C.Style.BOLD}Config dir:{C.Style.RESET} {C.Style.DIM}(e.g. ../infer_engines/vllm){C.Style.RESET}",
            default=self._last_config_dir,
        )
        self._status_msg = None

        if not config_dir or not config_dir.strip():
            self._set_status(f"{C.Style.DIM}Deploy cancelled.{C.Style.RESET}", 1.5)
            return

        config_dir = config_dir.strip()
        self._last_config_dir = config_dir
        abs_dir = os.path.join(self.deployer_dir, config_dir)

        # Validate
        user_json = os.path.join(abs_dir, "user_config.json")
        if not os.path.exists(user_json):
            self._last_config_dir = ""
            self._set_status(
                f"{C.Style.RED}✗ user_config.json not found in {abs_dir}{C.Style.RESET}",
                3.0,
            )
            return

        # Swap root logger handlers BEFORE any config loading/validation.
        import logging

        root = logging.getLogger()
        old_handlers = list(root.handlers)
        capture_buf = io.StringIO()
        capture_handler = logging.StreamHandler(capture_buf)
        capture_handler.setFormatter(old_handlers[0].formatter if old_handlers else None)
        root.handlers[:] = [capture_handler]

        captured_log: str = ""
        try:
            from lib.utils import read_json
            from lib.config_validator import resolve_config_paths, validate_pd_hybrid_config
            from lib.config_validator import validate_pd_hybrid_infer_service_template
            from lib.generator.engine import validate_instance_nums
            from lib.generator.k8s_utils import set_user_config_path

            user_config_path, env_config_path = resolve_config_paths(config_dir, None, None)
            set_user_config_path(user_config_path)
            os.makedirs(C.OUTPUT_ROOT_PATH, exist_ok=True)

            user_config = read_json(user_config_path)

            if C.HYBRID_INSTANCES_NUM in user_config.get(C.MOTOR_DEPLOY_CONFIG, {}):
                from lib.utils import get_deploy_paths

                validate_pd_hybrid_config(user_config)
                paths = get_deploy_paths()
                validate_pd_hybrid_infer_service_template(user_config, paths["infer_service_input_yaml"])
            validate_instance_nums(user_config)

            # Deploy
            self._set_status(f"{C.Style.BOLD}⏳ Deploying...{C.Style.RESET}", duration=60)
            import deploy as deploy_mod

            deploy_mod.deploy_services(user_config, env_config_path, dry_run=False, auto_log_collect=False)
        except Exception as e:
            root.handlers[:] = old_handlers
            self._set_status(f"{C.Style.RED}✗ Deploy failed: {e}{C.Style.RESET}", 3.0)
            return
        finally:
            root.handlers[:] = old_handlers
            captured_log = capture_buf.getvalue()

        # Save captured log to file
        if captured_log.strip():
            log_path = os.path.join(self.deployer_dir, "output_yamls", ".deploy.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(captured_log)

        # Update session state
        deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
        self.namespace = deploy_config.get(C.CONFIG_JOB_ID, self.namespace)
        self.pod_cnt = deploy_mod.calculate_pod_count(deploy_config)
        self.user_config = user_config
        self._deployed = True
        self._menu_selected = 0

        # Save log lines and immediately write them below the current box so
        # the user sees deploy output right away — no need to wait for the
        # next _menu_main render cycle.
        self._deploy_log_lines = captured_log.strip().split('\n')[-20:] if captured_log.strip() else []
        if self._ctx is not None and self._deploy_log_lines:
            log_start = self._last_box_end_row + 2
            for i, log_line in enumerate(self._deploy_log_lines[:15]):
                self._ctx._write(
                    f"\033[{log_start + i};1H  {C.Style.DIM}{log_line[: self.width - 4]}{C.Style.RESET}\033[K"
                )

        self._set_status(
            f"{C.Style.BOLD}{C.Style.GREEN}✓{C.Style.RESET}  "
            f"{C.Style.GREEN}Deployment done — namespace {C.Style.BOLD}{self.namespace}{C.Style.RESET}",
            10.0,
        )

    def _update_config(self) -> None:
        """Prompt for a config directory, then update ConfigMap."""
        ctx = self._ctx
        if ctx is None:
            return

        ctx.flush_input()
        self._status_msg = None

        self._set_status(
            f"{C.Style.BOLD}{C.Style.BLUE}[U]{C.Style.RESET} → "
            f"{C.Style.BOLD}Update config{C.Style.RESET}  —  "
            f"{C.Style.DIM}Enter config directory path{C.Style.RESET}",
            duration=60,
        )

        config_dir = self._read_line(
            f"{C.Style.BOLD}Config dir:{C.Style.RESET} {C.Style.DIM}(e.g. ../infer_engines/vllm){C.Style.RESET}",
            default=self._last_config_dir,
        )
        self._status_msg = None

        if not config_dir or not config_dir.strip():
            self._set_status(f"{C.Style.DIM}Update cancelled.{C.Style.RESET}", 1.5)
            return

        config_dir = config_dir.strip()
        self._last_config_dir = config_dir

        try:
            from lib.utils import read_json
            from lib.config_validator import resolve_config_paths

            user_config_path, _ = resolve_config_paths(config_dir, None, None)
            user_config = read_json(user_config_path)

            import deploy as deploy_mod

            deploy_mod.handle_update_config(user_config)
        except Exception as e:
            self._last_config_dir = ""
            self._set_status(f"{C.Style.RED}✗ Update failed: {e}{C.Style.RESET}", 3.0)
            return

        self._set_status(
            f"{C.Style.BOLD}{C.Style.GREEN}✓{C.Style.RESET}  {C.Style.GREEN}Config updated.{C.Style.RESET}",
            2.0,
        )

    # ------------------------------------------------------------------
    # Delete service (namespace input + confirmation)
    # ------------------------------------------------------------------

    def _delete_service(self) -> None:
        """Interactive service deletion with namespace input and confirm."""
        ctx = self._ctx
        if ctx is None:
            return

        # Step 1: input namespace
        ctx.flush_input()
        self._status_msg = None
        self._clear_confirm()

        self._set_status(
            f"{C.Style.BOLD}{C.Style.MAGENTA}[D]{C.Style.RESET} → "
            f"{C.Style.BOLD}Delete service{C.Style.RESET}  —  "
            f"{C.Style.DIM}Type namespace (Enter to confirm, Esc to cancel){C.Style.RESET}",
            duration=30,
        )

        ns = self._read_line(
            f"{C.Style.BOLD}Namespace:{C.Style.RESET}",
            default=self.namespace,
        )
        self._status_msg = None

        if not ns:
            self._set_status(f"{C.Style.DIM}Delete cancelled.{C.Style.RESET}", 1.5)
            return
        if not ns.strip():
            self._set_status(f"{C.Style.RED}✗ Namespace cannot be empty.{C.Style.RESET}", 2.0)
            return

        ns = ns.strip()

        # Step 2: confirmation (Y/N)
        self._set_status(
            f"{C.Style.BOLD}{C.Style.RED}⚠  WARNING:{C.Style.RESET}  "
            f"About to delete {C.Style.BOLD}ALL{C.Style.RESET} services in namespace "
            f"{C.Style.BOLD}{C.Style.RED}{ns}{C.Style.RESET}.  "
            f"Press {C.Style.BOLD}[Y]{C.Style.RESET} to proceed, "
            f"any other key to cancel.",
            duration=30,
        )

        ctx.flush_input()
        while True:
            key = ctx.poll_key(timeout=0.1)
            if key is None:
                continue
            if key in ('y', 'Y'):
                break
            self._status_msg = None
            self._set_status(f"{C.Style.DIM}Delete cancelled.{C.Style.RESET}", 1.5)
            return

        # Step 3: execute deletion
        self._status_msg = None
        self._set_status(
            f"{C.Style.BOLD}{C.Style.RED}▶{C.Style.RESET}  Executing deletion for {C.Style.BOLD}{ns}{C.Style.RESET}...",
            duration=60,
        )
        self._exec_delete(ns)

    def _exec_delete(self, ns: str) -> None:
        """Run the delete.sh script and stream its output live below the box."""
        ctx = self._ctx
        delete_sh = os.path.join(self.deployer_dir, "delete.sh")
        if not os.path.exists(delete_sh):
            self._set_status(
                f"{C.Style.RED}✗ delete.sh not found at {delete_sh}{C.Style.RESET}",
                3.0,
            )
            return

        try:
            with subprocess.Popen(
                ["/bin/bash", delete_sh, ns],
                cwd=self.deployer_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            ) as proc:
                output_lines: list[str] = []
                output_row = getattr(self, '_last_box_end_row', 10) + 1
                # Fit output in the space between box and terminal bottom
                term_h = shutil.get_terminal_size().lines
                max_display = max(5, min(50, term_h - output_row - 4))

                user_cancelled = False
                while self._running:
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                        k = ctx.poll_key(timeout=0)
                        if k in ('q', 'Q'):
                            try:
                                proc.terminate()
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=5)
                            except OSError:
                                pass
                            user_cancelled = True
                            break
                        continue

                    text = line.rstrip('\n')
                    # delete.sh countdown: printf "\r\033[K..." produces
                    # \r (consumed by Python as line terminator → empty line)
                    # then \033[K... (on the next readline).
                    # The FIRST \033[K after normal output starts a new line;
                    # subsequent \033[K overwrite that same line.
                    if not text:
                        continue
                    if text.startswith('\033[K'):
                        clean = text[3:]  # strip the \033[K prefix
                        if output_lines and self._in_delete_countdown:
                            output_lines[-1] = clean
                        else:
                            output_lines.append(clean)
                            self._in_delete_countdown = True
                    else:
                        output_lines.append(text)
                        self._in_delete_countdown = False
                    # Show tail that fits in the output window — all lines, scroll naturally
                    visible = output_lines[-max_display:]
                    for i, vline in enumerate(visible):
                        trimmed = vline[: self.width - 4]
                        ctx.write_at(
                            output_row + i,
                            1,
                            f"  {C.Style.DIM}{trimmed}{C.Style.RESET}\033[K",
                        )
                    ctx.move_to(output_row + len(visible), 1)

                # Clear all visible output lines
                for i in range(max_display + 1):
                    ctx.write_at(output_row + i, 1, '\033[K')
                rc = -1 if user_cancelled else proc.returncode
        except Exception as e:
            self._set_status(f"{C.Style.RED}✗ Failed to run delete.sh: {e}{C.Style.RESET}", 3.0)
            return

        if rc == 0:
            self._set_status(
                f"{C.Style.BOLD}{C.Style.GREEN}✓{C.Style.RESET}  "
                f"{C.Style.GREEN}Deletion completed for namespace "
                f"{C.Style.BOLD}{ns}{C.Style.RESET}.",
                3.0,
            )
        else:
            self._set_status(
                f"{C.Style.RED}✗ delete.sh exited with code {rc}.{C.Style.RESET}",
                3.0,
            )

        # After delete, reset to undeployed state and return to menu
        self._stop_progress_monitor()
        self._stop_pod_watcher()
        self._deployed = False
        self._progress_active = False
        self._pod_progress = PodProgressState()
        self.namespace = ""
        self.pod_cnt = 0
        self.user_config = {}
        self._log_running = False
        self._menu_selected = 0
        self._set_status(
            f"{C.Style.BOLD}{C.Style.GREEN}✓{C.Style.RESET}  "
            f"{C.Style.GREEN}Deletion done. Returning to menu...{C.Style.RESET}",
            2.0,
        )
