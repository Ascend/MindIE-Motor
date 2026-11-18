# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Private JSON-lines subprocess protocol; never imports unselected entry points."""

import inspect
import json
import sys
from dataclasses import asdict
from importlib.metadata import entry_points
from io import TextIOBase

from motor.coordinator.scheduler.policy.api import (
    SUPPORTED_POLICY_API_VERSION,
    CandidateId,
    CandidateSnapshot,
    EndpointSelectionPolicy,
    RequestContext,
    SelectionInput,
)

ENTRY_POINT_GROUP = "mindie_motor.scheduling_policies"
MAX_MESSAGE_BYTES = 1024 * 1024


def reject_json_constant(value: str) -> None:
    """Reject the non-standard numeric constants accepted by Python's JSON decoder."""
    raise ValueError("Invalid JSON numeric constant: %s" % value)


def _decode_line(line: bytes) -> dict:
    if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
        raise ValueError("Plugin input exceeds message limit or has no newline")
    return json.loads(line, parse_constant=reject_json_constant)


def load_policy(name: str, options: dict) -> EndpointSelectionPolicy:
    """Load exactly one configured class and validate its API contract."""
    entries = [entry for entry in entry_points(group=ENTRY_POINT_GROUP) if entry.name == name]
    if len(entries) != 1:
        raise ValueError("Expected exactly one entry point for %r, found %d" % (name, len(entries)))
    entry = entries[0]
    dist = getattr(entry, "dist", None)
    print(
        "Scheduling plugin source name=%s package=%s version=%s entry=%s"
        % (
            name,
            dist.metadata.get("Name", "?") if dist is not None else "?",
            dist.version if dist is not None else "?",
            entry.value,
        ),
        file=sys.stderr,
    )
    policy_class = entry.load()
    if not isinstance(policy_class, type) or not issubclass(policy_class, EndpointSelectionPolicy):
        raise ValueError("Entry point must be an EndpointSelectionPolicy subclass")
    if policy_class.api_version != SUPPORTED_POLICY_API_VERSION:
        raise ValueError("Incompatible scheduling policy API version")
    if (
        inspect.isabstract(policy_class)
        or not callable(policy_class.rank)
        or inspect.iscoroutinefunction(policy_class.rank)
    ):
        raise ValueError("Policy must override synchronous rank()")
    return policy_class(options=options)


def decode_selection(payload: dict) -> SelectionInput:
    """Rebuild only public DTOs from JSON; no live Motor objects cross the boundary."""
    candidates = tuple(CandidateSnapshot(**{**item, "id": CandidateId(**item["id"])}) for item in payload["candidates"])
    return SelectionInput(
        request=RequestContext(**payload["request"]),
        candidates=candidates,
        excluded=frozenset(CandidateId(**item) for item in payload["excluded"]),
        attempt=payload["attempt"],
    )


def main(protocol_output: TextIOBase | None = None) -> None:
    """Keep plugin stdout separate from protocol output, including constructor output."""
    if protocol_output is None:
        protocol_output = sys.stdout
    sys.stdout = sys.stderr

    def send(payload: dict) -> None:
        encoded = json.dumps(payload, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("Plugin response exceeds message limit")
        protocol_output.write(encoded + "\n")
        protocol_output.flush()

    try:
        config = _decode_line(sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1))
        policy = load_policy(config["name"], config["options"])
        send({"ready": True})
    except Exception as exc:
        send({"error": type(exc).__name__ + ": " + str(exc)[:512]})
        return
    while line := sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1):
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
            # End the protocol on a truncated frame rather than reading an unbounded tail.
            send({"error": "Plugin input exceeds message limit or has no newline"})
            return
        try:
            ranked = policy.rank(decode_selection(_decode_line(line)))
            send({"ranked": None if ranked is None else [asdict(item) for item in ranked]})
        except Exception as exc:
            send({"error": type(exc).__name__ + ": " + str(exc)[:512]})


if __name__ == "__main__":
    main()
