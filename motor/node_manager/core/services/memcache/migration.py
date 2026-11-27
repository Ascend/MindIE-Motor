# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Directed Memcache drain operations for a live standalone storage provider."""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from motor.common.logger import get_logger
from motor.common.utils.kv_migration import run_directed_drain

logger = get_logger(__name__)


class DrainSubmissionRejected(RuntimeError):
    """MetaService explicitly rejected the request without giving it a job UUID."""


class DrainNotStarted(ValueError):
    """This source has no recorded drain and its exit request cannot be submitted."""


def _confirmed_receipt(receipt: dict | None) -> dict:
    drain_id = receipt.get("drain_id") if isinstance(receipt, dict) else None
    if not isinstance(drain_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", drain_id
    ):
        raise RuntimeError("Drain has no acknowledged UUID; reconcile the recorded intent before retrying")
    return receipt


def _time_left(deadline: float | None, limit: float) -> float:
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("KV drain deadline reached")
    return min(limit, remaining)


def _create_record(record: Path, body: dict) -> None:
    record.parent.parent.mkdir(mode=0o700, exist_ok=True)
    record.parent.mkdir(mode=0o700, exist_ok=True)
    with record.open("x", encoding="utf-8") as stream:
        json.dump(body, stream)
        stream.flush()
        os.fsync(stream.fileno())


class MemcacheMigration:
    """Own the drain transport and durable receipt for one provider."""

    def __init__(
        self,
        api_url: str,
        live_storage_rank: Callable[[], tuple[subprocess.Popen, int]],
        plan_exit: Callable,
        worker_directory: Callable[[], str | None],
    ):
        self._drain_api_url = api_url
        self._live_storage_rank = live_storage_rank
        self._plan_exit = plan_exit
        self._worker_directory = worker_directory
        self._before_launch = uuid4().hex
        self._submission_lock = threading.Lock()

    def _record_path(self, uid: str, expected: Path | None = None) -> Path:
        root = Path(tempfile.gettempdir())
        directory = self._worker_directory()
        # Keep the journal outside the rank directory so it survives worker cleanup for reconciliation.
        worker = Path(directory).relative_to(root) if directory is not None else self._before_launch
        record = root / ("motor-drain-" + uid) / worker / "operation.json"
        if expected is not None and record != expected:
            raise RuntimeError("LocalService changed during exit preparation")
        return record

    def drain_inventory(self) -> dict[str, Any]:
        """Look up storage segments for the rank reported by the live LocalService."""
        label = os.environ.get("MMC_LOCAL_SERVICE_NODE_LABEL") or os.environ.get("POD_IP", "")
        uid = os.environ.get("POD_UID", "")
        if not label or not uid or not self._drain_api_url:
            raise RuntimeError("Cannot identify a live standalone source Pod")
        process, rank = self._live_storage_rank()
        response = requests.get(f"{self._drain_api_url}/api/v1/capacity/segment_remaining", timeout=3)
        response.raise_for_status()
        snapshot = response.json()
        if snapshot.get("data_source_ready") is not True or snapshot.get("degraded") is not False:
            raise RuntimeError("Memcache capacity snapshot is unavailable")
        has_rank = False
        for segment in snapshot["segments"]:
            name = segment["segment_name"]
            match = re.fullmatch(r"rank-(\d+)-\w+", name)
            if match and int(match.group(1)) == rank:
                has_rank = True
        if self._live_storage_rank() != (process, rank):
            raise RuntimeError("Standalone storage process changed during inventory")
        if not has_rank:
            raise RuntimeError("Provider has no verifiable segments")
        return {"uid": uid, "label": label, "pool": self._drain_api_url, "ranks": [rank]}

    def submit_directed_drain(
        self,
        operation_id: str,
        uid: str,
        targets: list[str],
        concurrency: int | None,
        *,
        exit_plan: dict | None = None,
        deadline: float | None = None,
        planned_record: Path | None = None,
    ):
        """Journal the intent before POST; an ambiguous submission is never sent twice."""
        if not uid or uid != os.environ.get("POD_UID") or not re.fullmatch(r"[a-zA-Z0-9-]+", uid):
            raise ValueError("Source Pod UID mismatch")
        if not targets or not operation_id:
            raise ValueError("Directed drain requires an operation id and targets")
        intent = {"operation_id": operation_id, "targets": sorted(set(targets)), "max_concurrency": concurrency}
        with self.submission_guard(deadline):
            record = self._record_path(uid, planned_record)
            if record.exists():
                previous = json.loads(record.read_text(encoding="utf-8"))
                # Reconfiguration and preStop may concurrently discover different targets for the same exit.
                prior_intent = previous.get("intent", {})
                same_exit = operation_id == "exit-" + uid and prior_intent.get("operation_id") == operation_id
                if previous.get("response") and (exit_plan is not None or same_exit or prior_intent == intent):
                    return _confirmed_receipt(previous["response"])
                raise RuntimeError("Source has another operation or an unacknowledged drain; reconcile first")
            if exit_plan is None:
                self.drain_inventory()
            else:
                _, rank = self._live_storage_rank()
                if exit_plan["source_uid"] != uid or [rank] != sorted(exit_plan["src_ranks"]):
                    raise ValueError("Source identity changed after exit planning")
            saved = {"intent": intent}
            _create_record(record, saved)
            try:
                timeout = _time_left(deadline, 10)
            except TimeoutError:
                record.unlink()
                raise
            logger.info(
                "Submitting Memcache drain: source_uid=%s operation_id=%s targets=%s record=%s",
                uid,
                operation_id,
                intent["targets"],
                record,
            )
            try:
                submitted = self.start_drain(intent["targets"], concurrency, timeout=timeout)
            except DrainSubmissionRejected:
                record.unlink()
                raise
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 400:
                    record.unlink()
                else:
                    logger.warning(
                        "Memcache drain outcome unknown for %s; reconcile intent at %s: %s", uid, record, exc
                    )
                raise
            pending = record.with_name("ack.tmp")
            with pending.open("w", encoding="utf-8") as stream:
                json.dump({**saved, "response": submitted}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(record)
            return submitted

    @contextmanager
    def submission_guard(self, deadline: float | None = None):
        """Serialize worker replacement with journal updates and submissions."""
        wait = -1 if deadline is None else max(0, deadline - time.monotonic())
        if not self._submission_lock.acquire(timeout=wait):  # pylint: disable=consider-using-with
            raise TimeoutError("Deadline reached while waiting for an in-flight drain submission")
        try:
            yield
        finally:
            self._submission_lock.release()

    def prepare_local_exit(self, uid: str, drained: bool, timeout: float) -> dict:
        """Wait for the native outcome of this Pod's new or previously acknowledged exit job."""
        if not uid or uid != os.environ.get("POD_UID") or not re.fullmatch(r"[a-zA-Z0-9-]+", uid):
            raise ValueError("Source Pod UID mismatch")
        deadline = time.monotonic() + timeout
        record = self._record_path(uid)
        result = run_directed_drain(
            lambda: self._get_or_submit_exit(uid, drained, deadline, record),
            deadline,
            query=self.query_drain,
            cancel=self.cancel_drain,
        )
        self._record_path(uid, record)
        return {**result, "source_uid": uid}

    def _get_or_submit_exit(self, uid: str, drained: bool, deadline: float, record: Path) -> dict:
        with self.submission_guard(deadline):
            self._record_path(uid, record)
            if record.exists():
                saved = json.loads(record.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and isinstance(saved.get("exit_rejected"), str):
                    raise DrainNotStarted(saved["exit_rejected"])
                return _confirmed_receipt(saved.get("response") if isinstance(saved, dict) else None)
        payload = {"source_ip": os.environ["POD_IP"], "source_uid": uid}
        if not drained:
            from motor.common.resources.endpoint import EndpointStatus
            from motor.node_manager.core.heartbeat_manager import HeartbeatManager

            endpoints = HeartbeatManager().get_active_endpoints(include_headless=True)
            if not endpoints or not all(ep.headless and ep.status == EndpointStatus.PAUSED for ep in endpoints):
                return self._exit_receipt_or_reject(
                    uid, record, deadline, "Cannot start a provider exit until requests drain"
                )
            payload["headless"] = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("KV migration budget exhausted before exit planning")
            plan = self._plan_exit({**payload, "timeout_sec": remaining}, timeout=remaining)
            if drained or not plan or plan.get("wait_for_ingress") is not True:
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            with self.submission_guard(deadline):
                self._record_path(uid, record)
                if record.exists():
                    return _confirmed_receipt(json.loads(record.read_text(encoding="utf-8")).get("response"))
        if (
            not plan
            or plan.get("source_uid") != uid
            or not all(plan.get(field) for field in ("src_ranks", "targets", "dst_ranks"))
            or set(plan["src_ranks"]).intersection(plan["dst_ranks"])
        ):
            return self._exit_receipt_or_reject(uid, record, deadline, "No verified provider exit targets")
        if not drained and plan.get("ingress_drained") is not True:
            return self._exit_receipt_or_reject(
                uid, record, deadline, "Controller did not confirm instance ingress drained"
            )
        return self.submit_directed_drain(
            "exit-" + uid, uid, plan["targets"], None, exit_plan=plan, deadline=deadline, planned_record=record
        )

    def _exit_receipt_or_reject(self, uid: str, record: Path, deadline: float, reason: str) -> dict:
        # Reconfiguration may have submitted while the exit planner was running.
        with self.submission_guard(deadline):
            self._record_path(uid, record)
            if record.exists():
                return _confirmed_receipt(json.loads(record.read_text(encoding="utf-8")).get("response"))
            # Freeze this retiring source before allowing its hook to finish. A later
            # recovery request must not create a drain after the definite rejection.
            _create_record(record, {"exit_rejected": reason})
            raise DrainNotStarted(reason)

    def start_drain(
        self,
        target_node_labels: list[str] | None = None,
        max_concurrency: int | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Submit an asynchronous Memcache drain and return its UUID."""
        node_label = os.environ.get("MMC_LOCAL_SERVICE_NODE_LABEL") or os.environ.get("POD_IP", "")
        if not self._drain_api_url or not node_label:
            raise RuntimeError("MMC_DRAIN_API_URL and MMC_LOCAL_SERVICE_NODE_LABEL (or POD_IP) are required")
        payload: dict[str, Any] = {"node_label": node_label}
        if target_node_labels:
            payload["target_node_labels"] = list(target_node_labels)
        if max_concurrency is not None:
            payload["max_concurrency"] = int(max_concurrency)
        response = requests.post(f"{self._drain_api_url}/api/v1/drain_jobs", json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Invalid Memcache submission response")
        if body.get("success") is False and not body.get("drain_id"):
            raise DrainSubmissionRejected("Memcache rejected drain submission")
        if body.get("success") is not True:
            raise RuntimeError("memcache drain submit response is missing a successful drain_id")
        drain_id = _confirmed_receipt(body)["drain_id"]
        result = dict(body)
        result.update({"state": "migrating", "source": "memcache_drain_api"})
        logger.info("Submitted memcache drain job: drain_id=%s node_label=%s", drain_id, node_label)
        return result

    def query_drain(self, drain_id: str, timeout: float = 5.0) -> dict[str, Any]:
        """Query one memcache drain by UUID and expose a Motor canonical state."""
        if not self._drain_api_url or not drain_id:
            raise RuntimeError("MMC_DRAIN_API_URL and drain_id are required")
        response = requests.get(
            f"{self._drain_api_url}/api/v1/drain_jobs/query",
            params={"drain_id": drain_id},
            timeout=min(timeout, 5.0),
        )
        response.raise_for_status()
        body = response.json()
        if body.get("drain_id", drain_id) != drain_id:
            raise RuntimeError("Memcache returned a different drain UUID")
        status_value = str(body.get("status", "unknown")).lower()
        state_map = {
            "submitted": "migrating",
            "running": "migrating",
            "cancelling": "migrating",
            "done": "migration_completed",
            "failed": "migration_failed",
            "cancelled": "migration_cancelled",
            "canceled": "migration_cancelled",
        }
        canonical_state = state_map.get(status_value, "migration_unknown")
        if canonical_state == "migration_completed":
            try:
                incomplete = any(
                    int(body.get(counter, 0) or 0)
                    for counter in ("failed_keys", "oom_keys", "skipped_keys", "cancelled_keys")
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid memcache drain failure counters") from exc
            if incomplete:
                canonical_state = "migration_failed"
        result = dict(body)
        result.update({"drain_id": drain_id, "state": canonical_state, "source": "memcache_drain_api"})
        return result

    def cancel_drain(self, drain_id: str, timeout: float = 5.0) -> dict[str, Any]:
        """Cancel one memcache drain by UUID."""
        if not self._drain_api_url or not drain_id:
            raise RuntimeError("MMC_DRAIN_API_URL and drain_id are required")
        response = requests.post(
            f"{self._drain_api_url}/api/v1/drain_jobs/cancel",
            json={"drain_id": drain_id},
            timeout=min(timeout, 5.0),
        )
        response.raise_for_status()
        body = response.json()
        if body.get("drain_id", drain_id) != drain_id:
            raise RuntimeError("Memcache returned a different drain UUID")
        if body.get("success") is not True or body.get("status") != "cancelling":
            raise RuntimeError("memcache drain cancel was not accepted")
        result = dict(body)
        result.update({"drain_id": drain_id, "state": "migrating", "source": "memcache_drain_api"})
        return result
