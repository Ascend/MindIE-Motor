# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Agentless whole-instance Docker orchestration using the native deployer."""

import argparse
import json
import sys
import time
from pathlib import Path

from lib.docker_orchestration.config import SpecError, load_spec
from lib.docker_orchestration.controller import Reconciler
from lib.docker_orchestration.executor import ExecutorError
from lib.docker_orchestration.management import ManagementError
from lib.docker_orchestration.state import StateError, StateStore


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate request key")
        result[key] = value
    return result


def _worker() -> int:
    from lib.docker_orchestration.native import handle_worker  # pylint: disable=import-outside-toplevel

    try:
        raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Worker request exceeds its limit")
        request = json.loads(raw, object_pairs_hook=_pairs)
        response = handle_worker(request)
    except (ValueError, UnicodeError):
        response = {"ok": False, "error": "Invalid worker JSON request", "may_have_effects": False}
    print(json.dumps(response, allow_nan=False, separators=(",", ":")))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Approved cluster JSON and native config directory")
    parser.add_argument("--state-dir", type=Path, help="Private local persistent state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Read current state and report proposed actions without reserving resources")
    commands.add_parser("status", help="Read persistent state without creating a database")
    apply = commands.add_parser("apply", help="Persist initial desired counts and run reconciliation")
    apply.add_argument("--wait", action="store_true", help="Continue until all requested instances are ready")
    commands.add_parser("run", help="Run the single controller; stopping it leaves model containers running")
    scale = commands.add_parser("scale", help="Persist explicit whole-instance counts")
    for role in ("prefill", "decode", "union"):
        scale.add_argument("--" + role, type=int)
    scale.add_argument("--wait", action="store_true", help="Reconcile until the requested counts are ready")
    commands.add_parser("stop", help="Set desired counts to zero, drain instances and remove owned containers")
    restart = commands.add_parser("restart", help="Explicitly restart one confirmed owned slot")
    restart.add_argument("--slot", required=True)
    commands.add_parser("worker", help=argparse.SUPPRESS)
    return parser


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)


def _wait(reconciler: Reconciler, *, continuous: bool) -> int:
    policy = reconciler.spec["policy"]
    deadline = time.monotonic() + policy["ready_timeout_seconds"] + policy["drain_timeout_seconds"]
    while True:
        try:
            result = reconciler.reconcile_once()
        except (ExecutorError, ManagementError, StateError) as error:
            if not continuous:
                raise
            _emit({"ok": False, "phase": "reconciliation_blocked", "error": str(error)})
        else:
            complete = reconciler.converged()
            _emit(
                {
                    "ok": True,
                    "phase": "converged" if complete else "reconciling",
                    "converged": complete,
                    "actions": result.get("actions", []),
                    "blocked": result.get("blocked", []),
                }
            )
            if complete and not continuous:
                return 0
        if not continuous and time.monotonic() >= deadline:
            raise StateError("Reconciliation deadline expired; inspect status and retained operation receipts")
        time.sleep(policy["interval_seconds"])


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "worker":
        return _worker()
    if args.config is None:
        raise SpecError("--config is required for controller commands")
    spec = load_spec(args.config)
    directory = args.state_dir or Path.home() / ".local" / "state" / "mindie-motor" / spec["name"]
    read_only = args.command in {"plan", "status"}
    store = StateStore(directory / "state.sqlite3", read_only=read_only)
    try:
        reconciler = Reconciler(spec, store, automatic_scaling=args.command == "run")
        if read_only:
            _emit(reconciler.preview() if args.command == "plan" else reconciler.status())
            return 0
        with store.writer_lock():
            store.configure(spec["digest"], spec["desired"])
            if args.command == "scale":
                desired = dict(store.snapshot()["desired"])
                provided = False
                for role in ("prefill", "decode", "union"):
                    count = getattr(args, role)
                    if count is not None:
                        if role not in desired:
                            raise SpecError("The requested role is not configured")
                        desired[role] = count
                        provided = True
                if not provided:
                    raise SpecError("scale requires at least one explicit role count")
                reconciler.set_desired(desired)
                if not args.wait:
                    _emit({"ok": True, "phase": "desired_saved", "desired": desired, "models_changed": False})
                    return 0
            elif args.command == "stop":
                reconciler.set_desired({role: 0 for role in store.snapshot()["desired"]})
            elif args.command == "restart":
                reconciler.restart(args.slot)
                return _wait(reconciler, continuous=False)
            if args.command == "run":
                return _wait(reconciler, continuous=True)
            if args.command == "stop" or getattr(args, "wait", False):
                return _wait(reconciler, continuous=False)
            result = reconciler.reconcile_once()
            complete = reconciler.converged()
            phase = "converged" if complete else "blocked" if result.get("blocked") else "reconciling"
            _emit({"ok": True, "phase": phase, "converged": complete, **result})
            return 0
    finally:
        store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _emit({"ok": True, "phase": "controller_stopped", "models_stopped": False})
        raise SystemExit(130) from None
    except (SpecError, StateError, ExecutorError, ManagementError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
