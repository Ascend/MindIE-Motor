# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Single-writer whole-instance reconciliation over the native Docker deployment path."""

# JSON booleans are not numeric resource counts or instance generations.
# pylint: disable=unidiomatic-typecheck

import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .executor import Executor, ExecutorError
from .management import ManagementError, NativeManagement
from .planner import plan
from .state import StateError, StateStore


class Reconciler:
    """Keep explicit desired counts without assuming a failed SSH path means a dead model."""

    def __init__(
        self,
        spec: dict,
        store: StateStore,
        *,
        executor: Executor | None = None,
        management: NativeManagement | None = None,
        automatic_scaling: bool = False,
    ) -> None:
        self.spec = spec
        self.store = store
        self.executor = executor or Executor()
        self.management = management or NativeManagement(spec)
        self.automatic_scaling = automatic_scaling
        self.observations: dict = {}
        self._last_scale = time.monotonic()
        self._scale_samples: dict = {}

    def _management_slot(self) -> dict:
        return {
            "id": "management",
            "role": "management",
            "host_id": self.spec["control_plane"]["host_id"],
            "devices": [],
            "ports": {},
        }

    def _slot(self, identifier: str) -> dict:
        if identifier == "management":
            return self._management_slot()
        for slot in self.spec["slots"]:
            if slot["id"] == identifier:
                return dict(slot)
        raise StateError("Instance slot is not in the selected specification")

    def _request(
        self,
        action: str,
        slot: dict | None = None,
        generation: int = 1,
        operation_id: str | None = None,
        instance: dict | None = None,
    ) -> dict:
        value = {
            "action": action,
            "cluster": self.spec["name"],
            "spec_digest": self.spec["digest"],
            "native_config": self.spec["_native_config"],
            "native_env": self.spec["_native_env"],
            "control_plane": self.spec["control_plane"],
            "generation": generation,
        }
        if slot is not None:
            value["slot"] = dict(slot)
            if slot["id"] == "management":
                value["slot"]["role"] = "coordinator_controller"
        if operation_id:
            value["operation_id"] = operation_id
        if instance:
            value["container_ids"] = list(instance["container_ids"])
            value["expected_machine_id"] = instance.get("host_machine_id")
            value["expected_daemon_id"] = instance.get("host_daemon_id")
        return value

    def _observe_host(self, item: tuple[str, dict]) -> tuple[str, dict]:
        identifier, host = item
        request = self._request("observe")
        request["devices"] = sorted(
            {device for slot in self.spec["slots"] if slot["host_id"] == identifier for device in slot["devices"]}
        )
        started = time.monotonic()
        timeout = min(25, max(1, self.spec["policy"]["observation_max_age_seconds"]))
        try:
            value = self.executor.call(host, request, timeout=timeout)
            if value.get("ok") is not True:
                raise ExecutorError("Host observation was not confirmed")
            value["received_at"] = time.time()
            value["elapsed_seconds"] = time.monotonic() - started
        except ExecutorError:
            value = {"ok": False, "error": "host_unavailable", "received_at": time.time()}
        return identifier, value

    def observe(self) -> dict:
        # Bounded parallel reads keep a slow host from aging every peer's sample.
        with ThreadPoolExecutor(max_workers=min(8, len(self.spec["hosts"]))) as pool:
            self.observations = dict(pool.map(self._observe_host, self.spec["hosts"].items()))
        return self.observations

    def status(self, *, observe: bool = False) -> dict:
        snapshot = self.store.snapshot()
        result = {
            "cluster": self.spec["name"],
            "spec_digest": self.spec["digest"],
            "revision": snapshot["revision"],
            "desired": snapshot.get("desired") or self.spec["desired"],
            "instances": snapshot["instances"],
            "operations": [
                {key: value.get(key) for key in ("operation_id", "action", "slot_id", "phase", "may_have_effects")}
                for value in snapshot["operations"].values()
            ],
        }
        if observe:
            result["observations"] = self.observe()
        return result

    def preview(self) -> dict:
        snapshot = self.store.snapshot()
        if snapshot.get("spec_digest") and snapshot["spec_digest"] != self.spec["digest"]:
            raise StateError("Specification differs from persistent state; review the existing deployment")
        result = plan(self.spec, snapshot, self.observe(), time.time())
        return {
            **result,
            "cluster": self.spec["name"],
            "spec_digest": self.spec["digest"],
            "revision": snapshot["revision"],
            "reserves_resources": False,
            "advisory": True,
            "final_admission_required": True,
        }

    def set_desired(self, counts: dict) -> None:
        snapshot = self.store.snapshot()
        previous = snapshot.get("desired") or self.spec["desired"]
        if set(counts) != set(previous):
            raise StateError("Scaling cannot change configured Motor roles")
        for role, count in counts.items():
            capacity = sum(slot["role"] == role for slot in self.spec["slots"])
            if type(count) is not int or not 0 <= count <= capacity:
                raise StateError("Desired count exceeds the approved candidate slot count")
        self.store.set_desired(counts, expected_revision=snapshot["revision"])
        self._last_scale = time.monotonic()
        self._scale_samples.clear()

    def _record(self, action: str, slot: dict, generation: int, result: dict, previous: dict | None) -> None:
        now = time.time()
        if action in {"deploy", "restart"}:
            value = {
                **(previous or {}),
                "slot_id": slot["id"],
                "role": slot["role"],
                "host_id": slot["host_id"],
                "generation": generation,
                "status": "starting",
                "job_name": result["job_name"],
                "container_ids": result["container_ids"],
                "node_manager_url": result.get("node_manager_url", ""),
                "reserved_ports": result["reserved_ports"],
                "spec_digest": self.spec["digest"],
                "host_machine_id": result["machine_id"],
                "host_daemon_id": result["daemon_id"],
                "last_change": now,
                "created_at": now,
                "last_operation_id": result["operation_id"],
                "restart_attempts": (previous or {}).get("restart_attempts", 0) + 1 if action == "restart" else 0,
            }
            for key in ("coordinator_url", "coordinator_metrics_url", "controller_url"):
                if result.get(key):
                    value[key] = result[key]
            self.store.put_instance(slot["id"], value)
            if result.get("coordinator_metrics_url"):
                self.management.metrics_url = result["coordinator_metrics_url"]
            return
        if action == "stop":
            self.store.put_instance(
                slot["id"],
                {**previous, "status": "stopped", "last_change": now, "last_operation_id": result["operation_id"]},
            )
        if action == "remove":
            self.store.release(
                slot["id"],
                {
                    "operation_id": result["operation_id"],
                    "confirmed": True,
                    "executor_terminal": True,
                    "may_have_effects": False,
                    "status": "removed",
                    "container_ids": previous["container_ids"],
                },
            )
            self.store.put_instance(
                slot["id"],
                {**previous, "status": "removed", "last_change": now, "last_operation_id": result["operation_id"]},
            )

    def _execute(self, action: str, slot: dict, generation: int, previous: dict | None = None) -> dict:
        host = self.spec["hosts"][slot["host_id"]]
        observation = self.observations.get(slot["host_id"], {})
        if observation.get("ok") is not True:
            raise StateError("Host is unavailable; resource ownership is retained")
        expected = {"expected_machine_id": observation["machine_id"], "expected_daemon_id": observation["daemon_id"]}
        if previous and (
            previous["host_machine_id"] != observation["machine_id"]
            or previous["host_daemon_id"] != observation["daemon_id"]
        ):
            raise StateError("Host identity changed; explicit recovery review is required")
        planning = self._request("plan", slot, generation)
        planning.update(expected)
        prepared = self.executor.call(host, planning)
        ports = prepared.get("reserved_ports")
        if not isinstance(ports, list) or not ports:
            raise StateError("Native plan did not provide a complete port set")
        if prepared.get("coordinator_url", "").rstrip("/") != self.management.origin:
            raise StateError("Configured Coordinator origin differs from the native deployment")
        slot = {
            **slot,
            "_reserved_ports": ports,
            "_machine_id": observation["machine_id"],
            "_daemon_id": observation["daemon_id"],
        }
        operation_id = uuid.uuid4().hex
        request = self._request(action, slot, generation, operation_id, previous)
        request.update(expected)
        intent = {
            "action": action,
            "slot_id": slot["id"],
            "generation": generation,
            "spec_digest": self.spec["digest"],
            "container_ids": request.get("container_ids", []),
        }
        snapshot = self.store.snapshot()
        self.store.prepare(operation_id, intent, slot, expected_revision=snapshot["revision"])
        self.store.submitted(operation_id)
        try:
            result = self.executor.call(host, request, timeout=240)
        except ExecutorError as error:
            # Even a failed SSH client cannot undo a command already accepted by the server.
            receipt = getattr(error, "receipt", None)
            no_effect = not error.outcome_unknown and self._no_effect(receipt, request, action)
            self.store.failed(
                operation_id, str(error), outcome_unknown=not no_effect, result=receipt if no_effect else None
            )
            if no_effect and action == "deploy":
                self.store.release(
                    slot["id"],
                    {
                        "operation_id": operation_id,
                        "confirmed": True,
                        "executor_terminal": True,
                        "may_have_effects": False,
                        "status": "not_created",
                        "container_ids": [],
                    },
                )
            raise
        if not self._success(result, request, action):
            self.store.failed(operation_id, "Incomplete execution receipt", outcome_unknown=True)
            raise StateError("Execution result could not be bound to the exact operation")
        self.store.complete(operation_id, result)
        self._record(action, slot, generation, result, previous)
        return result

    def _no_effect(self, receipt: dict | None, request: dict, action: str) -> bool:
        if not isinstance(receipt, dict):
            return False
        expected = {
            "operation_id": request["operation_id"],
            "spec_digest": self.spec["digest"],
            "slot_id": request["slot"]["id"],
            "generation": request["generation"],
            "action": action,
            "cluster": self.spec["name"],
            "host_id": request["slot"]["host_id"],
            "machine_id": request["expected_machine_id"],
            "daemon_id": request["expected_daemon_id"],
            "container_ids": request.get("container_ids", []),
        }
        return (
            type(receipt.get("generation")) is int
            and all(receipt.get(key) == value for key, value in expected.items())
            and receipt.get("phase") == "failed_no_effect"
            and receipt.get("ok") is False
            and receipt.get("outcome_unknown") is False
            and receipt.get("may_have_effects") is False
            and receipt.get("executor_terminal") is True
        )

    def _success(self, receipt: dict, request: dict, action: str) -> bool:
        expected = {
            "operation_id": request["operation_id"],
            "action": action,
            "cluster": self.spec["name"],
            "spec_digest": self.spec["digest"],
            "slot_id": request["slot"]["id"],
            "generation": request["generation"],
            "machine_id": request["expected_machine_id"],
            "daemon_id": request["expected_daemon_id"],
        }
        if (
            not isinstance(receipt, dict)
            or receipt.get("ok") is not True
            or type(receipt.get("generation")) is not int
            or any(receipt.get(key) != value for key, value in expected.items())
        ):
            return False
        identifiers = receipt.get("container_ids")
        if (
            not isinstance(identifiers, list)
            or len(identifiers) != 1
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in identifiers)
        ):
            return False
        if receipt.get("reserved_ports") != sorted(request["slot"]["_reserved_ports"]):
            return False
        if not isinstance(receipt.get("job_name"), str) or not receipt["job_name"]:
            return False
        status = {"deploy": "starting", "restart": "starting", "stop": "stopped", "remove": "removed"}[action]
        if receipt.get("status") != status or receipt.get("phase") != ("submitted" if status == "starting" else status):
            return False
        if action != "deploy" and identifiers != request.get("container_ids"):
            return False
        if action in {"stop", "remove"} and receipt.get("may_have_effects") is not False:
            return False
        return action != "remove" or receipt.get("remaining_container_ids") == []

    def _recovery_request(self, operation: dict, claim: dict, previous: dict | None) -> dict:
        slot = {**self._slot(operation["slot_id"]), "_reserved_ports": claim["ports"]}
        request = self._request("operation", slot, operation["generation"], operation["operation_id"])
        request.update(expected_machine_id=claim["machine_id"], expected_daemon_id=claim["daemon_id"])
        request["container_ids"] = operation["intent"].get("container_ids", [])
        if "container_ids" not in operation["intent"] and operation["action"] != "deploy" and previous:
            request["container_ids"] = previous["container_ids"]
        return request

    def _next_generation(self, slot_id: str) -> int:
        snapshot = self.store.snapshot()
        previous = [
            value.get("generation", 0) for value in snapshot["operations"].values() if value.get("slot_id") == slot_id
        ]
        previous.append(snapshot["instances"].get(slot_id, {}).get("generation", 0))
        previous.append(snapshot.get("claims", {}).get(slot_id, {}).get("generation", 0))
        return max(previous) + 1

    def _resolve(self) -> bool:
        snapshot = self.store.snapshot()
        # The claim points at the latest operation for this slot. Replaying only
        # that durable result repairs a crash between receipt and instance commits.
        for claim in snapshot.get("claims", {}).values():
            if claim["state"] == "released":
                continue
            value = snapshot["operations"].get(claim["operation_id"])
            previous = snapshot["instances"].get(claim["slot_id"])
            if (
                value
                and value["phase"] == "completed"
                and (not previous or previous.get("last_operation_id") != value["operation_id"])
            ):
                request = self._recovery_request(value, claim, previous)
                if not self._success(value["result"], request, value["action"]):
                    raise StateError("Stored execution receipt does not match its resource claim")
                self._record(
                    value["action"], self._slot(value["slot_id"]), value["generation"], value["result"], previous
                )
        snapshot = self.store.snapshot()
        for operation_id, value in snapshot["operations"].items():
            if value["phase"] == "prepared":
                # Submission is persisted before SSH is invoked. A prepared-only
                # intent therefore has no remote effects, including after a crash.
                self.store.failed(operation_id, "Controller stopped before submission", outcome_unknown=False)
                if value["action"] == "deploy":
                    self.store.release(
                        value["slot_id"],
                        {
                            "operation_id": operation_id,
                            "confirmed": True,
                            "executor_terminal": True,
                            "may_have_effects": False,
                            "status": "not_created",
                            "container_ids": [],
                        },
                    )
                continue
            if value["phase"] not in {"submitted", "unknown"} and not (
                value["phase"] == "failed" and value.get("may_have_effects") is not False
            ):
                continue
            slot = self._slot(value["slot_id"])
            claim = snapshot["claims"].get(slot["id"], {})
            previous = snapshot["instances"].get(slot["id"])
            request = self._recovery_request(value, claim, previous)
            try:
                result = self.executor.call(self.spec["hosts"][slot["host_id"]], request)
            except ExecutorError as error:
                if error.outcome_unknown or not self._no_effect(error.receipt, request, value["action"]):
                    return False
                self.store.failed(operation_id, str(error), outcome_unknown=False, result=error.receipt)
                if value["action"] == "deploy":
                    self.store.release(
                        slot["id"],
                        {
                            "operation_id": operation_id,
                            "confirmed": True,
                            "executor_terminal": True,
                            "may_have_effects": False,
                            "status": "not_created",
                            "container_ids": [],
                        },
                    )
                continue
            if result.get("outcome_unknown") or result.get("phase") in {"not_found", "inflight", "unknown"}:
                return False
            if value["phase"] == "failed" or not self._success(result, request, value["action"]):
                return False
            self.store.complete(operation_id, result)
            self._record(value["action"], slot, value["generation"], result, previous)
        return True

    def _owned_running(self, value: dict) -> bool:
        observation = self.observations.get(value["host_id"], {})
        if (
            observation.get("ok") is not True
            or observation.get("machine_id") != value["host_machine_id"]
            or observation.get("daemon_id") != value["host_daemon_id"]
        ):
            return False
        rows = {row["id"]: row for row in observation.get("containers", [])}
        return bool(value["container_ids"]) and all(
            identifier in rows
            and rows[identifier].get("running") is True
            and rows[identifier].get("slot_id") == value["slot_id"]
            and rows[identifier].get("generation") == value["generation"]
            and rows[identifier].get("spec_digest") == self.spec["digest"]
            and rows[identifier].get("job_name") == value["job_name"]
            for identifier in value["container_ids"]
        )

    def _update_readiness(self) -> None:
        for slot_id, value in self.store.snapshot()["instances"].items():
            if value["status"] not in {"starting", "ready", "unready"}:
                continue
            if value.get("coordinator_metrics_url"):
                self.management.metrics_url = value["coordinator_metrics_url"]
            try:
                if slot_id == "management":
                    self.management.metrics_url = value.get("coordinator_metrics_url")
                    self.management.instances()
                    self.management.request(value["controller_url"].rstrip("/") + "/readiness")
                    ready = self._owned_running(value)
                else:
                    ready = self._owned_running(value) and self.management.ready(value)
            except ManagementError:
                ready = False
            if ready and value["status"] != "ready":
                self.store.put_instance(slot_id, {**value, "status": "ready", "last_change": time.time()})
            elif (
                not ready
                and value["status"] != "unready"
                and (
                    value["status"] == "ready"
                    or time.time() - value["created_at"] > self.spec["policy"]["ready_timeout_seconds"]
                )
            ):
                self.store.put_instance(slot_id, {**value, "status": "unready", "last_change": time.time()})

    def restart(self, slot_id: str) -> None:
        self.observe()
        value = self.store.snapshot()["instances"].get(slot_id)
        if not value or value["status"] in {"removed", "released", "unknown"}:
            raise StateError("No confirmed owned instance is available for restart")
        self._execute("restart", self._slot(slot_id), value["generation"], value)

    def _resume_interrupted_drains(self) -> None:
        snapshot = self.store.snapshot()
        for slot_id, value in snapshot["instances"].items():
            if value["status"] != "draining":
                continue
            current = sum(
                row["role"] == value["role"] and row["status"] not in {"removed", "released"}
                for row in snapshot["instances"].values()
            )
            if current <= snapshot["desired"].get(value["role"], 0):
                self.management.resume(value)
                self.store.put_instance(
                    slot_id, {**value, "status": "starting", "last_change": time.time(), "created_at": time.time()}
                )

    def _drain(self, slot_id: str) -> None:
        value = self.store.snapshot()["instances"][slot_id]
        slot = self._slot(slot_id)
        if value["status"] not in {"stopped", "removed"}:
            self.store.put_instance(slot_id, {**value, "status": "draining", "last_change": time.time()})
            # The durable phase survives an interrupted pause or failed rollback.
            # A later pass must explicitly resume/drain; it is never Ready.
            self.management.drain(value)
            value = self.store.snapshot()["instances"][slot_id]
            self._execute("stop", slot, value["generation"], value)
        value = self.store.snapshot()["instances"][slot_id]
        self._execute("remove", slot, value["generation"], value)

    def _autoscale(self) -> None:
        policy = self.spec["autoscaling"]
        if (
            not self.automatic_scaling
            or not policy["enabled"]
            or time.monotonic() - self._last_scale < policy["cooldown_seconds"]
        ):
            return
        snapshot = self.store.snapshot()
        live = [
            value
            for value in snapshot["instances"].values()
            if value["role"] != "management" and value["status"] not in {"removed", "released"}
        ]
        if not live or any(value["status"] != "ready" for value in live):
            self._scale_samples.clear()
            return
        counts = dict(snapshot["desired"])
        scale_in = policy.get("scale_in", set(counts) == {"union"})
        for role, rule in policy["roles"].items():
            try:
                values = self.management.role_counts(role)
            except ManagementError:
                values = None
            if values is None:
                self._scale_samples.pop(role, None)
                continue
            direction = (
                1
                if values["running"] + values["waiting"] > rule["up_running"]
                else -1
                if scale_in and values["running"] + values["waiting"] <= rule["down_running"]
                else 0
            )
            previous_direction, samples = self._scale_samples.get(role, (0, 0))
            samples = samples + 1 if direction == previous_direction else 1
            self._scale_samples[role] = (direction, samples)
            if direction and samples >= policy["stable_samples"]:
                target = max(rule["min"], min(rule["max"], counts[role] + direction))
                if target != counts[role]:
                    counts[role] = target
                    self.set_desired(counts)
                    return

    def reconcile_once(self) -> dict:
        self.observe()
        if not self._resolve():
            return {"actions": [], "blocked": [{"reason": "unresolved_operation"}]}
        snapshot = self.store.snapshot()
        counts = snapshot["desired"]
        managed = self.spec["control_plane"]["mode"] == "managed"
        management = snapshot["instances"].get("management")
        if managed and any(counts.values()) and management and management["status"] == "stopped":
            self._execute("remove", self._management_slot(), management["generation"], management)
            return {"actions": [{"action": "retire_management"}], "blocked": []}
        if managed and any(counts.values()) and (not management or management["status"] in {"removed", "released"}):
            generation = self._next_generation("management")
            self._execute("deploy", self._management_slot(), generation)
            return {"actions": [{"action": "create_management"}], "blocked": []}
        self._resume_interrupted_drains()
        self._update_readiness()
        snapshot = self.store.snapshot()
        management = snapshot["instances"].get("management")
        if managed and any(counts.values()) and management["status"] != "ready":
            observation = self.observations.get(management["host_id"], {})
            rows = {row["id"]: row for row in observation.get("containers", [])}
            terminal = [rows[key] for key in management["container_ids"] if key in rows]
            if (
                terminal
                and len(terminal) == len(management["container_ids"])
                and all(row.get("status") in {"exited", "dead"} for row in terminal)
                and any(
                    row.get("oom_killed") is True
                    or type(row.get("exit_code")) is int
                    and row["exit_code"] not in {0, 143}
                    for row in terminal
                )
            ):
                if (
                    management.get("restart_attempts", 0) < self.spec["policy"]["retry_limit"]
                    and time.time() - management["last_change"] >= self.spec["policy"]["retry_backoff_seconds"]
                ):
                    self._execute("restart", self._management_slot(), management["generation"], management)
            return {"actions": [], "blocked": [{"reason": "management_not_ready"}]}
        self._autoscale()
        snapshot = self.store.snapshot()
        result = plan(self.spec, snapshot, self.observations, time.time())
        if result["actions"]:
            action = result["actions"][0]
            slot_id = action["slot_id"]
            slot = self._slot(slot_id)
            previous = snapshot["instances"].get(slot_id)
            if action["action"] == "create":
                generation = self._next_generation(slot_id)
                self._execute("deploy", slot, generation)
            elif action["action"] == "drain":
                self._drain(slot_id)
            else:
                attempts = previous.get("restart_attempts", 0)
                if attempts >= self.spec["policy"]["retry_limit"]:
                    result["blocked"].append({"slot_id": slot_id, "reason": "recovery_budget_exhausted"})
                elif time.time() - previous["last_change"] >= self.spec["policy"]["retry_backoff_seconds"]:
                    self._execute("restart", slot, previous["generation"], previous)
        elif (
            managed
            and not any(snapshot["desired"].values())
            and management
            and management["status"] not in {"removed", "released"}
            and all(
                value["status"] in {"removed", "released"}
                for key, value in snapshot["instances"].items()
                if key != "management"
            )
        ):
            if management["status"] != "stopped":
                self._execute("stop", self._management_slot(), management["generation"], management)
            management = self.store.snapshot()["instances"]["management"]
            self._execute("remove", self._management_slot(), management["generation"], management)
        return result

    def converged(self) -> bool:
        snapshot = self.store.snapshot()
        management = snapshot["instances"].get("management")
        if self.spec["control_plane"]["mode"] == "managed" and any(snapshot["desired"].values()):
            if not management or management["status"] != "ready":
                return False
        for role, count in snapshot["desired"].items():
            values = [
                value
                for value in snapshot["instances"].values()
                if value["role"] == role and value["status"] not in {"removed", "released"}
            ]
            if len(values) != count or any(value["status"] != "ready" for value in values):
                return False
        if not any(snapshot["desired"].values()) and self.spec["control_plane"]["mode"] == "managed":
            management = snapshot["instances"].get("management")
            if management and management["status"] not in {"removed", "released"}:
                return False
        return not any(
            value["phase"] in {"prepared", "submitted", "unknown"}
            or value["phase"] == "failed"
            and value.get("may_have_effects") is not False
            for value in snapshot["operations"].values()
        )
