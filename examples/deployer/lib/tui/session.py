# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Core interactive TUI session class."""

from __future__ import annotations

import os
import threading
import time

from lib import constant as C
from .actions import _DeployActionsMixin
from .state import PodProgressState
from .terminal import TerminalContext
from .tui_utils import draw_box, format_progress_bar


class DeployInteractiveSession(_DeployActionsMixin):
    """Post-deployment interactive TUI.

    Manages the terminal UI lifecycle: terminal raw-mode switching,
    main menu rendering with inline progress bars, keyboard navigation
    (arrow keys + vim-style h/j/k/l), and status/feedback display.
    """

    # (key, color, label_fn_or_str, action_fn)
    _MENU_DEFS: list[tuple] = []  # built in __init__

    def __init__(
        self,
        namespace: str,
        pod_cnt: int,
        user_config: dict,
        log_running: bool = False,
        deployed: bool = True,
    ):
        self.namespace = namespace
        self.pod_cnt = pod_cnt
        self.user_config = user_config
        self.deployer_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

        # Core state
        self._running = False
        self._log_running = log_running
        self._deployed = deployed
        self._cancel_event = threading.Event()
        self._monitor_threads: list[threading.Thread] = []
        self._pod_progress = PodProgressState()

        # Status / feedback state
        self._status_msg: str | None = None
        self._status_expiry: float = 0.0
        self._confirm_action: str | None = None
        self._confirm_expiry: float = 0.0

        # Menu navigation state
        self._menu_selected: int = 0
        self._flash_item: int | None = None
        self._flash_expiry: float = 0.0

        # Terminal
        self._ctx: TerminalContext | None = None
        self._width: int = C.MIN_BOX_WIDTH
        self._last_box_end_row: int = 10
        self._progress_selected: int = 0
        self._progress_active: bool = False
        self._deploy_log_lines: list[str] = []

        # Cache last-used config path for deploy / update_config prompts
        self._last_config_dir: str = ""

        # Track delete countdown state for correct line overwrite
        self._in_delete_countdown: bool = False

        # Pod status overview (kubectl get pods)
        self._pod_status: dict[str, str] = {}  # pod_name → (ready, status) str
        self._pod_status_lock = threading.Lock()
        self._pod_watcher_stop = threading.Event()
        self._pod_watcher_thread: threading.Thread | None = None

    @property
    def width(self) -> int:
        return self._width

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Enter interactive session (blocks until user exits)."""
        with TerminalContext() as ctx:
            self._ctx = ctx
            self._width = ctx.width
            self._running = True
            while self._running:
                self._menu_main()
        self._cleanup()

    # ------------------------------------------------------------------
    # Status / feedback
    # ------------------------------------------------------------------

    def _set_status(self, msg: str, duration: float = C.STATUS_DURATION) -> None:
        self._status_msg = msg
        self._status_expiry = time.monotonic() + duration
        self._flush_status()

    def _set_confirm(self, action: str) -> None:
        self._confirm_action = action
        self._confirm_expiry = time.monotonic() + C.CONFIRM_DURATION

    def _clear_confirm(self) -> None:
        self._confirm_action = None
        self._confirm_expiry = 0.0

    def _flush_status(self) -> None:
        """Immediately write the current status line to the terminal."""
        if self._ctx is None:
            return
        row = getattr(self, '_last_box_end_row', 10)
        status = self._build_status_line(row)
        if status:
            self._ctx._write(f'\033[{row + 1};1H{status}')
        else:
            self._ctx._write(f'\033[{row + 1};1H\033[K')

    def _render_status_line(self, box_end_row: int) -> None:
        """Render the status / feedback line below the box."""
        if self._ctx is None:
            return
        self._last_box_end_row = box_end_row
        status = self._build_status_line(box_end_row)
        if status:
            self._ctx._write(f'\033[{box_end_row + 1};1H{status}')
        else:
            self._ctx._write(f'\033[{box_end_row + 1};1H\033[K')

    # ------------------------------------------------------------------
    # Key reading (handles escape sequences for arrow keys)
    # ------------------------------------------------------------------

    def _read_key(self, timeout: float = 0.08) -> str | None:
        """Read a single keystroke.  Arrow keys and vim-style ``j/k`` are
        normalised to ``'UP'`` / ``'DOWN'``.
        Returns ``None`` on timeout.
        """
        ctx = self._ctx
        if ctx is None:
            return None

        key = ctx.poll_key(timeout=timeout)
        if key is None:
            return None

        # vim-style navigation: j/k → DOWN/UP  (NOT h/l — they collide with direct keys)
        if key == 'j':
            return 'DOWN'
        if key == 'k':
            return 'UP'

        if key != '\033':
            return key

        # Escape sequence — collect remaining bytes (up to 5 more)
        seq = ''
        for _ in range(5):
            nxt = ctx.poll_key(timeout=0.03)
            if nxt is None:
                break
            seq += nxt

        if seq == '[A':
            return 'UP'
        if seq == '[B':
            return 'DOWN'
        if seq == '[C':
            return 'RIGHT'
        if seq == '[D':
            return 'LEFT'
        return '\033'  # plain Escape or unknown sequence

    # ------------------------------------------------------------------
    # Main menu
    # ------------------------------------------------------------------

    def _menu_main(self) -> None:
        """Render the main menu — adapts to deployed / undeployed state."""
        ctx = self._ctx
        ctx.flush_input()
        self._clear_confirm()
        self._status_msg = None
        self._width = ctx.width
        self._menu_selected = 0
        self._flash_item = None
        self._flash_expiry = 0.0
        _repoll_deadline = 0.0
        _progress_retry_deadline = 0.0

        while self._running:
            now = time.monotonic()

            # Pod status watcher — always on when deployed
            if self._deployed and self._pod_watcher_thread is None:
                self._start_pod_watcher()

            # Auto-start progress monitoring (except single-container mode)
            dep_cfg = self.user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
            is_single = dep_cfg.get(C.DEPLOY_MODE_CONFIG_KEY, "") == C.DEPLOY_MODE_SINGLE_CONTAINER
            if (
                self._deployed
                and not self._progress_active
                and not is_single
                and self.pod_cnt > 0
                and now >= _progress_retry_deadline
            ):
                self._start_progress_monitor()
                # Short retry (5 s) if pods not ready yet; _rescan_pods
                # handles incremental discovery once progress is active.
                _progress_retry_deadline = now + 5

            # Periodic re-scan for pods that appeared after initial discovery
            if self._progress_active and hasattr(self, '_progress_monitor') and now >= _repoll_deadline:
                self._rescan_pods()
                _repoll_deadline = now + 5

            # Build the item list dynamically
            if self._deployed:
                log_status = (
                    f"{C.Style.GREEN}● Running{C.Style.RESET}"
                    if self._log_running
                    else f"{C.Style.DIM}○ Stopped{C.Style.RESET}"
                )
                log_action = "Restart log collection" if self._log_running else "Start log collection"

                if is_single:
                    total_pods = self.pod_cnt
                    pod_info = f"{total_pods} (single-container x{total_pods})"
                else:
                    standby = dep_cfg.get(C.STANDBY_CONFIG, {}).get(C.ENABLE_MASTER_STANDBY)
                    coord_cnt = 2 if standby else 1
                    total_pods = self.pod_cnt + 1 + coord_cnt  # engines + controller + coordinator(s)
                    pod_info = f"{total_pods} (controller:1 coordinator:{coord_cnt} engine:{self.pod_cnt})"

                # Build menu items — no progress monitoring for single-container
                if is_single:
                    items = [
                        ('L', C.Style.YELLOW, log_action, f"{C.Style.GRAY}({log_status}){C.Style.RESET}"),
                        ('U', C.Style.BLUE, 'Update config', ''),
                        ('D', C.Style.RED, 'Delete service', ''),
                        ('Q', C.Style.RED, 'Exit', ''),
                    ]
                else:
                    prog_status = (
                        f"{C.Style.GREEN}● Showing{C.Style.RESET}"
                        if self._progress_active
                        else f"{C.Style.DIM}○ Idle{C.Style.RESET}"
                    )
                    prog_label = "Close progress" if self._progress_active else "Show startup progress"
                    items = [
                        ('P', C.Style.GREEN, prog_label, f"{C.Style.GRAY}({prog_status}){C.Style.RESET}"),
                        ('L', C.Style.YELLOW, log_action, f"{C.Style.GRAY}({log_status}){C.Style.RESET}"),
                        ('U', C.Style.BLUE, 'Update config', ''),
                        ('D', C.Style.RED, 'Delete service', ''),
                        ('Q', C.Style.RED, 'Exit', ''),
                    ]

                footer_hint = (
                    "j/k (↑↓) to navigate  Enter to select  or press [L] [U] [D] [Q]"
                    if is_single
                    else "j/k (↑↓) to navigate  Enter to select  or press [P] [L] [U] [D] [Q]"
                )

                header = [
                    "",
                    f"  Namespace:      {C.Style.BOLD}{self.namespace}{C.Style.RESET}",
                    f"  Pods expected:  {C.Style.BOLD}{pod_info}{C.Style.RESET}",
                    f"  Log collector:  {log_status}",
                    "",
                ]
            else:
                items = [
                    ('R', C.Style.GREEN, 'Deploy services', ''),
                    ('Q', C.Style.RED, 'Exit', ''),
                ]
                header = [
                    "",
                    f"  {C.Style.DIM}No services deployed.{C.Style.RESET}",
                    f"  {C.Style.DIM}Use [R] to deploy from a config directory.{C.Style.RESET}",
                    "",
                ]
                footer_hint = "j/k (↑↓) to navigate  Enter to select  or press [R] [Q]"

            n_items = len(items)

            # Helper to format one menu row
            def _item_line(idx: int, key: str, color: str, label: str, extra: str = "") -> str:
                is_sel = idx == self._menu_selected
                is_flash = idx == self._flash_item
                if is_flash:
                    cursor = f"{C.Style.BLINK}{C.Style.REVERSE} ▶ {C.Style.RESET}"
                    kpart = f"{C.Style.REVERSE}{C.Style.BOLD}{color}{C.Style.BLINK}[{key}]{C.Style.RESET}"
                    text = f"{C.Style.REVERSE}{C.Style.BOLD} {label} {extra} {C.Style.RESET}"
                elif is_sel:
                    cursor = f"{C.Style.BLINK}{C.Style.CYAN}▶{C.Style.RESET} "
                    kpart = f"{C.Style.BOLD}{color}[{key}]{C.Style.RESET}"
                    text = f" {label} {extra}"
                else:
                    cursor = "  "
                    kpart = f"{C.Style.BOLD}{color}[{key}]{C.Style.RESET}"
                    text = f" {label} {extra}"
                return f"{cursor}{kpart}{text}"

            body = list(header)

            # Pod status overview (kubectl get pods) — always visible when deployed
            if self._deployed:
                with self._pod_status_lock:
                    status_snapshot = dict(self._pod_status)
                if status_snapshot:
                    max_name = max((len(n) for n in status_snapshot), default=30)
                    pad_w = min(max_name + 2, 55)
                    body.append("")
                    body.append(f"  {C.Style.BOLD}Pod Status:{C.Style.RESET}")
                    body.append(
                        f"    {'NAME':^{pad_w}}{'READY':>5s}  {'STATUS':<10s}"
                        f"  {'RESTARTS':>8s}  {'AGE':>5s}  {'POD IP':<15s}  NODE"
                    )
                    for pod_name in sorted(status_snapshot):
                        info = status_snapshot[pod_name]
                        display_name = pod_name[:pad_w].ljust(pad_w)
                        body.append(f"    {C.Style.DIM}{display_name}{C.Style.RESET}{info}")
                    body.append("")

            # Inline progress bars when active
            if self._deployed and self._progress_active:
                pods = self._pod_progress.get_all()
                if pods:
                    # static_overhead: fixed columns either side of bar+label
                    # "  " + label + "  " + bar + "  " + pct(4) + "  " + time(13) + "  " + Line:(9) + "  " + Err:N(6)
                    static_overhead = 45
                    usable = self.width - 4 - static_overhead
                    max_name = max((len(n) for n in pods.keys()), default=20)
                    label_w = min(max_name, max(10, usable - 10), 25)
                    bar_w = max(8, usable - label_w)
                    for pod_name in sorted(pods.keys()):
                        ps = pods[pod_name]
                        line = format_progress_bar(
                            ps["progress"],
                            pod_name[:label_w].ljust(label_w),
                            bar_w,
                            line_cnt=ps["line_index"],
                            err_cnt=ps["error_count"],
                            elapsed=now - ps.get("started_at", now),
                        )
                        body.append(line)
                else:
                    body.append(f"  {C.Style.DIM}Waiting for pods...{C.Style.RESET}")
                body.append("")

            for i, (dk, col, lbl, xtra) in enumerate(items):
                body.append(_item_line(i, dk, col, lbl, xtra))
            body.append("")

            footer = [
                f"  {C.Style.DIM}{footer_hint}{C.Style.RESET}",
            ]

            lines = draw_box("Deployer", body, self.width, footer_lines=footer, double_border=True)
            self._render_screen(lines)

            # Mid-flash — keep rendering
            if self._flash_item is not None:
                if now >= self._flash_expiry:
                    action_idx = self._flash_item
                    self._flash_item = None
                    self._execute_menu_action(action_idx, items)
                else:
                    time.sleep(0.06)
                continue

            key = self._read_key(timeout=0.08)
            if key is None:
                continue

            if key == 'UP':
                self._menu_selected = (self._menu_selected - 1) % n_items
            elif key == 'DOWN':
                self._menu_selected = (self._menu_selected + 1) % n_items
            elif key == '\r':  # Enter
                self._flash_item = self._menu_selected
                self._flash_expiry = time.monotonic() + C.FLASH_DURATION
            elif key == '\033':  # Escape — cancel confirm
                if self._confirm_action:
                    self._clear_confirm()
            else:
                # Direct key matching
                matched = False
                for i, (dk, _col, _lbl, _xtra) in enumerate(items):
                    if key.lower() == dk.lower():
                        self._flash_item = i
                        self._flash_expiry = time.monotonic() + C.FLASH_DURATION
                        matched = True
                        break
                if not matched and self._confirm_action:
                    self._clear_confirm()

    def _execute_menu_action(self, idx: int, items: list[tuple]) -> None:
        """Execute the action for menu item *idx*, passing its direct-key."""
        dk = items[idx][0]

        if self._deployed:
            mapping = {
                'P': self._toggle_progress,
                'L': self._handle_log_key,
                'U': self._update_config,
                'D': self._delete_service,
                'Q': lambda: setattr(self, '_running', False),
            }
        else:
            mapping = {
                'R': self._deploy_service,
                'Q': lambda: setattr(self, '_running', False),
            }

        action = mapping.get(dk)
        if action is None:
            return

        # Clear deploy logs before executing any action.
        self._deploy_log_lines = []
        # Force-clear the area immediately so old logs don't linger on screen
        # while the action (delete / deploy / update) is in progress.
        if self._ctx is not None:
            log_start = self._last_box_end_row + 2
            self._ctx._write(f"\033[{log_start};1H\033[J")

        key_label = f"{C.Style.BOLD}{C.Style.CYAN}[{dk}]{C.Style.RESET}"
        if dk == 'Q':
            self._set_status(f"{key_label} → {C.Style.RED}Exiting...{C.Style.RESET}", 0.5)
            time.sleep(0.05)
        elif dk == 'P':
            label = "Closed" if self._progress_active else "Opened"
            self._set_status(f"{key_label} → {C.Style.GREEN}Progress {label}{C.Style.RESET}", 1.5)
        elif dk == 'R':
            self._set_status(f"{key_label} → {C.Style.GREEN}Deploy services...{C.Style.RESET}", 1.5)

        action()

        # Wipe residual content before menu re-render (skip deploy — logs stay)
        if dk not in ('Q', 'R') and self._ctx is not None:
            self._ctx.clear()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_screen(self, lines: list[str]) -> None:
        """Write *lines* to terminal in a single batch, plus deploy logs."""
        ctx = self._ctx
        parts = []
        for i, line in enumerate(lines):
            parts.append(f'\033[{i + 1};1H{line}')
        box_end = len(lines)
        # Status line
        status = self._build_status_line(box_end)
        if status:
            parts.append(f'\033[{box_end + 1};1H{status}')
        else:
            parts.append(f'\033[{box_end + 1};1H\033[K')

        # Deploy log lines — render with wrapping, or clear the area
        log_start = box_end + 2
        if self._deploy_log_lines:
            max_w = self.width - 4
            row = log_start
            for log_line in self._deploy_log_lines:
                # Wrap long lines at max_w
                while len(log_line) > max_w:
                    parts.append(f"\033[{row};1H  {C.Style.DIM}{log_line[:max_w]}{C.Style.RESET}\033[K")
                    log_line = log_line[max_w:]
                    row += 1
                    if row >= log_start + 30:  # limit total lines
                        break
                if row >= log_start + 30:
                    break
                parts.append(f"\033[{row};1H  {C.Style.DIM}{log_line}{C.Style.RESET}\033[K")
                row += 1
        else:
            # Wipe stale log lines from previous render
            parts.append(f'\033[{log_start};1H\033[J')

        # Hide cursor
        parts.append(f'\033[{box_end + 3};1H')
        ctx._write(''.join(parts))

    def _build_status_line(self, box_end_row: int) -> str:
        """Return the status-line string (without cursor positioning)."""
        if self._ctx is None:
            return ''
        self._last_box_end_row = box_end_row
        now = time.monotonic()

        # Expire stale
        if self._status_msg and now >= self._status_expiry:
            self._status_msg = None
        if self._confirm_action and now >= self._confirm_expiry:
            self._clear_confirm()
            self._set_status(f"{C.Style.DIM}Confirmation timed out — cancelled.{C.Style.RESET}", 1.2)
            return self._build_status_line(box_end_row)

        if self._confirm_action:
            action_label = {
                'log_restart': 'restart log collection',
                'log_stop': 'stop log collection',
            }.get(self._confirm_action, self._confirm_action)
            remaining = max(0, self._confirm_expiry - now)
            return (
                f"  {C.Style.BOLD}{C.Style.ORANGE}⚠  Confirm:{C.Style.RESET}  "
                f"Log collector is already running.  "
                f"Press {C.Style.BOLD}{C.Style.YELLOW}[L]{C.Style.RESET} again to "
                f"{C.Style.BOLD}{action_label}{C.Style.RESET}  "
                f"({C.Style.DIM}{remaining:.0f}s{C.Style.RESET})  —  "
                f"{C.Style.DIM}any other key cancels{C.Style.RESET}\033[K"
            )
        elif self._status_msg:
            return f"  {self._status_msg}\033[K"
        return ''

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_monitors(self) -> None:
        self._cancel_event.set()
        for t in self._monitor_threads:
            t.join(timeout=3)
        self._monitor_threads.clear()

    def _cleanup(self) -> None:
        if self._progress_active:
            self._stop_progress_monitor()
        self._stop_pod_watcher()
        if self._log_running:
            self._stop_log_collection()
