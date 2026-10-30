# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Ask before remapping Docker engine ports that are already bound on the host.

Same answers as the Kubernetes NodePort prompt: y auto-assigns a free port,
a number uses that port, N aborts. A non-interactive stdin keeps today's
preflight failure instead of blocking on input.

Only engine listen ports that already have CLI overrides are offered
(node-manager, data-parallel-rpc, base-port, kv-port). The chosen value is
written onto the override object and ``args`` so the workspace copy and the
in-container ``--start`` command both see it. ``user_config.json`` is not modified.
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from dataclasses import dataclass

import lib.docker_utils as D

_KIND_ORDER = ("node-manager", "data-parallel-rpc", "base", "kv")
_KIND_PROMPT = {
    "node-manager": "node-manager",
    "data-parallel-rpc": "data-parallel-rpc",
    "base": "base-port",
    "kv": "kv-port",
}
_KIND_FIELD = {
    "node-manager": "node_manager_port",
    "data-parallel-rpc": "data_parallel_rpc_port",
    "base": "base_port",
    "kv": "kv_port",
}


@dataclass
class _Span:
    kind: str
    anchor: int
    span: int
    labels: dict[int, str]


def _kind_of(label: str) -> str | None:
    if label in ("node-manager", "data-parallel-rpc"):
        return label
    if label.startswith("engine-service[") or label.startswith("engine-mgmt["):
        return "base"
    if label.startswith("kv["):
        return "kv"
    return None


def _index_of(label: str) -> int:
    return int(label[label.index("[") + 1 : -1])


def _make_span(kind: str, items: list[tuple[str, int]]) -> _Span:
    labels = {port: label for label, port in items}
    if kind in ("node-manager", "data-parallel-rpc"):
        return _Span(kind, items[0][1], 1, labels)
    count = max(_index_of(label) for label, _ in items) + 1
    if kind == "base":
        anchor = next((port for label, port in items if label == "engine-service[0]"), None)
        if anchor is None:
            mgmt0 = next((port for label, port in items if label == "engine-mgmt[0]"), None)
            anchor = (mgmt0 - 1) if mgmt0 is not None else min(port for _, port in items)
        return _Span(kind, anchor, count * 2, labels)
    anchor = next((port for label, port in items if label == "kv[0]"), None)
    if anchor is None:
        anchor = min(port for _, port in items)
    return _Span(kind, anchor, count, labels)


def _spans(ports: list[tuple[str, int]]) -> list[_Span]:
    grouped: dict[str, list[tuple[str, int]]] = {kind: [] for kind in _KIND_ORDER}
    for label, port in ports:
        kind = _kind_of(label)
        if kind is not None:
            grouped[kind].append((label, port))
    return [_make_span(kind, grouped[kind]) for kind in _KIND_ORDER if grouped[kind]]


def _current_ports(user_config: dict, role: str, overrides: D.EnginePortOverrides) -> list[tuple[str, int]]:
    config = user_config
    if overrides.specified():
        config = copy.deepcopy(user_config)
        D.apply_engine_port_overrides(config, role, overrides)
    return D.collect_engine_listen_ports(config, role)


def _busy_port(span: _Span, in_use: Callable[[int], bool]) -> int | None:
    for port in range(span.anchor, span.anchor + span.span):
        if in_use(port):
            return port
    return None


def _blocked(spans: list[_Span], kind: str, reserved: dict[int, str]) -> dict[int, str]:
    blocked = dict(reserved)
    for span in spans:
        if span.kind == kind:
            continue
        name = _KIND_PROMPT[span.kind]
        for port in range(span.anchor, span.anchor + span.span):
            blocked.setdefault(port, name)
    return blocked


def _holder(describe: Callable[[int], str], port: int) -> str:
    try:
        return (describe(port) or "").strip()
    except OSError:
        return ""


def _reject_reason(
    port: int,
    span: int,
    blocked: dict[int, str],
    in_use: Callable[[int], bool],
    describe: Callable[[int], str],
) -> str | None:
    if port < 1 or port + span - 1 > 65535:
        return f"{port} is out of range; the {span}-port block must fit in 1-65535."
    for offset in range(span):
        candidate = port + offset
        owner = blocked.get(candidate)
        if owner:
            return f"{candidate} overlaps {owner}."
        if in_use(candidate):
            holder = _holder(describe, candidate)
            suffix = " by: " + holder if holder else ""
            return f"{candidate} is already in use{suffix}."
    return None


def _auto_port(span: _Span, blocked: dict[int, str], in_use: Callable[[int], bool]) -> int | None:
    last = 65535 - span.span + 1
    if last < 1:
        return None
    start = span.anchor + 1
    windows = []
    if start <= last:
        windows.append(range(max(start, 1), last + 1))
    low_end = min(max(start, 1), last + 1)
    if low_end > 1024:
        windows.append(range(1024, low_end))

    def silent(_port: int) -> str:
        return ""

    for window in windows:
        for port in window:
            if _reject_reason(port, span.span, blocked, in_use, silent) is None:
                return port
    return None


def _store(overrides: D.EnginePortOverrides, args, kind: str, port: int) -> None:
    field = _KIND_FIELD[kind]
    setattr(overrides, field, port)
    if args is not None:
        setattr(args, field, port)


def _prompt_span(
    span: _Span,
    busy: int,
    spans: list[_Span],
    reserved: dict[int, str],
    in_use: Callable[[int], bool],
    describe: Callable[[int], str],
    ask: Callable[[str], str],
) -> int | None:
    prompt_name = _KIND_PROMPT[span.kind]
    label = span.labels.get(busy, prompt_name)
    holder = _holder(describe, busy)
    suffix = " by: " + holder if holder else ""
    D.logger.info("[Port] CONFLICT: %s port %s is already in use%s.", label, busy, suffix)
    if span.span > 1:
        D.logger.info(
            "[Port] %s moves the whole range %s-%s.",
            prompt_name,
            span.anchor,
            span.anchor + span.span - 1,
        )
    D.logger.info("[Port] Enter y=auto-assign, <port>=use that port, N=abort.")
    blocked = _blocked(spans, span.kind, reserved)
    while True:
        sys.stderr.flush()
        try:
            answer = ask(f"[Port] {span.anchor} ({prompt_name}) -> y / <port> / N: ").strip()
        except EOFError:
            D.logger.error("[Port] no interactive input; aborting remap for %s port %s.", prompt_name, span.anchor)
            return None
        lowered = answer.lower()
        if lowered in ("", "n", "no"):
            D.logger.error("[Port] aborting; %s port %s is still in use.", prompt_name, span.anchor)
            return None
        if lowered in ("y", "yes"):
            new_port = _auto_port(span, blocked, in_use)
            if new_port is None:
                D.logger.error("[Port] no free port for %s. Enter a port number, or N to abort.", prompt_name)
                continue
            return new_port
        if answer.isdigit():
            new_port = int(answer)
            reason = _reject_reason(new_port, span.span, blocked, in_use, describe)
            if reason:
                D.logger.error("[Port] %s", reason)
                continue
            return new_port
        D.logger.error("[Port] invalid input %r. Enter y, a port number, or N.", answer)


def offer_engine_port_remap(
    user_config: dict,
    role: str,
    overrides: D.EnginePortOverrides,
    args=None,
    *,
    port_in_use_fn: Callable[[int], bool] | None = None,
    describe_fn: Callable[[int], str] | None = None,
    input_fn: Callable[[str], str] | None = None,
    isatty_fn: Callable[[], bool] | None = None,
) -> bool:
    """Prompt once per conflicting engine port kind. Return False when the user aborts.

    ``y`` picks the next free port that does not overlap another listen range of this
    command. A number uses that port (base-port and kv-port are the start of the range).
    ``N``, empty input, EOF, or a non-TTY stdin leaves the conflict in place.
    """
    if not D.engine_role_of(role):
        return True
    in_use = port_in_use_fn or D.port_in_use
    describe = describe_fn or D.describe_port_holder
    ask = input_fn or input
    tty = isatty_fn or sys.stdin.isatty
    ctrl, obs = D.docker_controller_ports(user_config)
    reserved = {ctrl: "controller", obs: "controller-obs"}
    while True:
        spans = _spans(_current_ports(user_config, role, overrides))
        conflict = None
        for span in spans:
            busy = _busy_port(span, in_use)
            if busy is not None:
                conflict = (span, busy)
                break
        if conflict is None:
            return True
        span, busy = conflict
        if not tty():
            D.logger.error(
                "[Port] stdin is not a TTY; %s port %s stays in use. Re-run in a terminal and answer y to auto-assign.",
                _KIND_PROMPT[span.kind],
                busy,
            )
            return False
        chosen = _prompt_span(span, busy, spans, reserved, in_use, describe, ask)
        if chosen is None:
            return False
        _store(overrides, args, span.kind, chosen)
        D.logger.info("[Port] %s %s -> %s", _KIND_PROMPT[span.kind], span.anchor, chosen)
