# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Local single-writer state; uncertain remote effects never expire into capacity.

The writer lock coordinates this controller's local data directory only. It is
neither a lock on other Docker users nor fencing of an unreachable workload.
"""

# Exact JSON scalar types deliberately reject bool where integers are required.
# pylint: disable=unidiomatic-typecheck

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Any, Iterator

from .config import digest


class StateError(ValueError):
    """State ownership, intent or resource identity could not be established."""


_APPLICATION = 0x444F434F
_SCHEMA = 1
_PENDING = {"prepared", "submitted", "unknown"}
_CREATING = {"deploy", "create", "recover"}
_LIFECYCLE = {"stop", "remove", "restart", "pause", "resume", "drain"}


def _json(value: object) -> str:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise StateError("State input must be finite JSON") from error
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise StateError("State record exceeds its byte limit")
    return text


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise StateError(field + " is invalid")
    return value


def _counts(value: object) -> dict:
    if not isinstance(value, dict):
        raise StateError("Desired counts are invalid")
    if (
        not value
        or set(value) - {"prefill", "decode", "union"}
        or "union" in value
        and len(value) != 1
        or any(type(count) is not int or not 0 <= count <= 128 for count in value.values())
    ):
        raise StateError("Desired counts are invalid")
    return dict(value)


def _container_ids(value: dict) -> list:
    raw = value.get("container_ids")
    if raw is None:
        rows = value.get("containers", [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise StateError("Container observations are invalid")
        raw = [row.get("id") for row in rows]
    if not isinstance(raw, list) or len(raw) > 256:
        raise StateError("Exact container identities are invalid")
    result = [_identifier(item, "container_id") for item in raw]
    if len(set(result)) != len(result):
        raise StateError("Container identities are duplicated")
    return sorted(result)


class StateStore:
    """Persist one controller's specification, operations and complete slot claims.

    Write methods require ``writer_lock`` for the controller's whole lifetime.
    A read-only store never initializes a missing database or parent directory.
    SQLite WAL must reside on a local filesystem, not a shared network volume.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path).expanduser().absolute()
        self.read_only = read_only
        self._db = None
        self._writer = None
        self._depth = 0
        self._mutex = threading.RLock()
        self._db_read_only = False

    @contextmanager
    def writer_lock(self) -> Iterator[StateStore]:
        """Hold a kernel lock, never steal ownership based on timestamps or PIDs."""
        if self.read_only:
            raise StateError("Read-only state cannot acquire a writer lock")
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".writer.lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        locked = False
        try:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or lock_path.is_symlink()
                or before.st_ino != lock_path.stat().st_ino
            ):
                raise StateError("Writer lock must be an independent regular file")
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif os.name == "nt":
                    import msvcrt

                    if before.st_size == 0:
                        handle.write(b"\0")
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    raise StateError("No supported local writer lock exists for this OS")
            except OSError as error:
                raise StateError("Another controller holds this state writer lock") from error
            locked = True
            self._writer, self._depth = handle, 1
            yield self
        finally:
            try:
                with self._mutex:
                    try:
                        self.close()
                    finally:
                        self._writer, self._depth = None, 0
            finally:
                try:
                    if locked:
                        if os.name == "posix":
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                        elif os.name == "nt":
                            import msvcrt

                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    handle.close()

    def _connection(self, *, write: bool = False) -> sqlite3.Connection | None:
        if write and (self.read_only or self._writer is None or not self._depth):
            raise StateError("Mutation requires the controller lifetime writer lock")
        if write:
            current = os.fstat(self._writer.fileno())
            lock_path = self.path.with_name(self.path.name + ".writer.lock")
            named = lock_path.lstat()
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or (named.st_dev, named.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise StateError("Writer lock pathname was replaced")
        if self._db is not None and write and self._db_read_only:
            self._db.close()
            self._db = None
        if self._db is not None:
            return self._db
        if self.path.is_symlink():
            raise StateError("State database must not be a symbolic link")
        exists = self.path.exists()
        if not exists and not write:
            return None
        if exists and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise StateError("State database must be an independent regular file")
        readonly = self.read_only or self._writer is None
        uri = self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw" if exists else "?mode=rwc")
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            if not exists:
                os.chmod(self.path, 0o600)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                    "phase TEXT NOT NULL, body TEXT NOT NULL)"
                )
                connection.execute("CREATE TABLE claims (id TEXT PRIMARY KEY, state TEXT NOT NULL, body TEXT NOT NULL)")
                connection.execute("CREATE TABLE instances (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
                for key, value in {"revision": 0, "desired": {}, "spec_digest": ""}.items():
                    connection.execute("INSERT INTO metadata VALUES (?,?)", (key, _json(value)))
                connection.execute("PRAGMA application_id=" + str(_APPLICATION))
                connection.execute("PRAGMA user_version=" + str(_SCHEMA))
                connection.execute("COMMIT")
            if (
                connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION
                or connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA
            ):
                raise StateError("State database belongs to another application or unsupported schema")
            if readonly:
                connection.execute("PRAGMA query_only=ON")
            else:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
            self._db = connection
            self._db_read_only = readonly
            return connection
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            db = self._connection(write=True)
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _meta(db: sqlite3.Connection, key: str) -> Any:
        return json.loads(db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0])

    @staticmethod
    def _set(db: sqlite3.Connection, key: str, value: object) -> None:
        db.execute("UPDATE metadata SET value=? WHERE key=?", (_json(value), key))

    def _touch(self, db: sqlite3.Connection) -> int:
        revision = self._meta(db, "revision") + 1
        self._set(db, "revision", revision)
        return revision

    def _expect(self, db: sqlite3.Connection, revision: int | None) -> None:
        if revision is not None and (type(revision) is not int or revision != self._meta(db, "revision")):
            raise StateError("State revision changed; generate a new plan")

    @staticmethod
    def _read(db: sqlite3.Connection, table: str, identifier: str) -> dict | None:
        queries = {
            "operations": "SELECT body FROM operations WHERE id=?",
            "instances": "SELECT body FROM instances WHERE id=?",
            "claims": "SELECT body FROM claims WHERE id=?",
        }
        if table not in queries:
            raise StateError("Unsupported state table")
        row = db.execute(queries[table], (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put_operation(db: sqlite3.Connection, value: dict) -> None:
        db.execute(
            "UPDATE operations SET phase=?,body=? WHERE id=?", (value["phase"], _json(value), value["operation_id"])
        )

    @staticmethod
    def _put_claim(db: sqlite3.Connection, value: dict) -> None:
        db.execute(
            "INSERT INTO claims VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, body=excluded.body",
            (value["slot_id"], value["state"], _json(value)),
        )

    def configure(self, spec_digest: str, desired: dict) -> None:
        if not isinstance(spec_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", spec_digest):
            raise StateError("Specification digest is invalid")
        counts = _counts(desired)
        with self._transaction() as db:
            previous = self._meta(db, "spec_digest")
            if previous == spec_digest:
                return  # A restart must not undo a persisted scale decision.
            active = db.execute("SELECT 1 FROM claims WHERE state<>'released' LIMIT 1").fetchone()
            pending = db.execute("SELECT 1 FROM operations WHERE phase IN ('prepared','submitted','unknown') LIMIT 1")
            if previous and (active or pending.fetchone()):
                raise StateError("Cannot rebind an active specification or unresolved remote effects")
            self._set(db, "spec_digest", spec_digest)
            self._set(db, "desired", counts)
            self._touch(db)

    def snapshot(self) -> dict:
        with self._mutex:
            db = self._connection()
            if db is None:
                return {
                    "revision": 0,
                    "desired": {},
                    "instances": {},
                    "operations": {},
                    "claims": {},
                    "spec_digest": "",
                }
            db.execute("BEGIN")
            try:
                result = {key: self._meta(db, key) for key in ("revision", "desired", "spec_digest")}
                queries = {
                    "instances": "SELECT id,body FROM instances LIMIT 100001",
                    "operations": "SELECT id,body FROM operations LIMIT 100001",
                    "claims": "SELECT id,body FROM claims LIMIT 100001",
                }
                for table, query in queries.items():
                    rows = db.execute(query).fetchall()
                    if len(rows) > 100000:
                        raise StateError("State snapshot exceeds its supported record limit")
                    result[table] = {row["id"]: json.loads(row["body"]) for row in rows}
                db.execute("COMMIT")
                return result
            except Exception:
                db.execute("ROLLBACK")
                raise

    def set_desired(self, counts: dict, *, expected_revision: int | None = None) -> int:
        counts = _counts(counts)
        with self._transaction() as db:
            self._expect(db, expected_revision)
            previous = self._meta(db, "desired")
            if set(counts) != set(previous):
                raise StateError("Scaling cannot change configured instance roles")
            if counts == previous:
                return self._meta(db, "revision")
            self._set(db, "desired", counts)
            return self._touch(db)

    def prepare(self, operation_id: str, intent: dict, slot: dict, *, expected_revision: int) -> dict:
        operation_id = _identifier(operation_id, "operation_id")
        if not isinstance(intent, dict) or not isinstance(slot, dict):
            raise StateError("Operation intent and slot must be objects")
        intent, slot = json.loads(_json(intent)), json.loads(_json(slot))
        slot_id = _identifier(slot.get("id", slot.get("slot_id")), "slot_id")
        action = intent.get("action")
        if not isinstance(action, str) or action not in _CREATING | _LIFECYCLE:
            raise StateError("Unsupported operation action")
        generation = intent.get("generation")
        if type(generation) is not int or generation < 1:
            raise StateError("Operation generation must be a positive integer")
        if intent.get("slot_id", slot_id) != slot_id:
            raise StateError("Intent and slot identify different targets")
        host_id = _identifier(slot.get("host_id"), "host_id")
        devices, ports = slot.get("devices"), slot.get("_reserved_ports")
        management = slot_id == "management" and slot.get("role") in {"management", "coordinator,controller"}
        if not isinstance(devices, list):
            raise StateError("Slot must identify unique explicit devices")
        if (
            not devices
            and not management
            or len(devices) > 256
            or any(type(value) is not int or not 0 <= value <= 65535 for value in devices)
            or len(set(devices)) != len(devices)
        ):
            raise StateError("Slot must identify unique explicit devices")
        if (
            not isinstance(ports, list)
            or not ports
            or len(ports) > 1024
            or any(type(value) is not int or not 1 <= value <= 65535 for value in ports)
            or len(set(ports)) != len(ports)
        ):
            raise StateError("A complete native-derived _reserved_ports list is required")
        declared_ports = slot.get("ports", {})
        if (
            not isinstance(declared_ports, dict)
            or any(type(value) is not int for value in declared_ports.values())
            or not set(declared_ports.values()) <= set(ports)
        ):
            raise StateError("Native-derived port reservation does not cover the explicit slot ports")
        for key in ("_machine_id", "_daemon_id"):
            value = slot.get(key, "")
            if (
                not isinstance(value, str)
                or len(value) > 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise StateError("Host observation identity is invalid")
        claim = {
            "slot_id": slot_id,
            "host_id": host_id,
            "role": slot.get("role"),
            "generation": generation,
            "devices": sorted(devices),
            "ports": sorted(ports),
            "operation_id": operation_id,
            "machine_id": slot.get("_machine_id", ""),
            "daemon_id": slot.get("_daemon_id", ""),
        }
        intent_digest = digest(intent)
        fingerprint = digest({"intent": intent, "claim": claim})
        with self._transaction() as db:
            row = db.execute("SELECT fingerprint,body FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise StateError("Operation ID is already bound to a different intent or resource group")
                return json.loads(row["body"])
            self._expect(db, expected_revision)
            binding = self._meta(db, "spec_digest")
            if not binding or intent.get("spec_digest") != binding:
                raise StateError("Operation does not match the configured specification")
            for row in db.execute("SELECT body FROM claims WHERE state<>'released'").fetchall():
                other = json.loads(row[0])
                if other["slot_id"] == slot_id:
                    previous = self._read(db, "operations", other["operation_id"])
                    unresolved = previous and previous["phase"] in _PENDING
                    resources_changed = any(other[key] != claim[key] for key in ("host_id", "devices", "ports"))
                    identity_changed = any(
                        other.get(key) and other[key] != claim[key] for key in ("machine_id", "daemon_id")
                    )
                    if (
                        action not in _LIFECYCLE
                        or other["generation"] != generation
                        or unresolved
                        or resources_changed
                        or identity_changed
                    ):
                        raise StateError("Slot still has an active or unresolved resource group")
                    continue
                same_host = (
                    other["host_id"] == host_id
                    or bool(claim["machine_id"])
                    and other.get("machine_id") == claim["machine_id"]
                )
                if same_host and (set(other["devices"]) & set(devices) or set(other["ports"]) & set(ports)):
                    raise StateError("An approved slot overlaps an existing resource claim")
            previous_claim = self._read(db, "claims", slot_id)
            if (
                previous_claim
                and previous_claim["state"] == "released"
                and action in _CREATING
                and generation <= previous_claim["generation"]
            ):
                raise StateError("A new instance must use a newer generation")
            value = {
                "operation_id": operation_id,
                "intent_digest": intent_digest,
                "intent": intent,
                "slot_id": slot_id,
                "generation": generation,
                "action": action,
                "phase": "prepared",
                "may_have_effects": False,
                "result": None,
                "error": None,
                "submitted_at": None,
                "updated_at": time.time(),
            }
            db.execute("INSERT INTO operations VALUES (?,?,?,?)", (operation_id, fingerprint, "prepared", _json(value)))
            self._put_claim(db, {**claim, "state": "held"})
            self._touch(db)
            return json.loads(_json(value))

    def submitted(self, operation_id: str) -> None:
        with self._transaction() as db:
            value = self._read(db, "operations", operation_id)
            if not value or value["phase"] not in {"prepared", "submitted"}:
                raise StateError("Only a prepared operation can be submitted")
            if value["phase"] == "submitted":
                return
            value.update(phase="submitted", may_have_effects=True, submitted_at=time.time(), updated_at=time.time())
            self._put_operation(db, value)
            self._touch(db)

    def complete(self, operation_id: str, result: dict) -> None:
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise StateError("Completion requires a successful exact execution result")
        _json(result)
        with self._transaction() as db:
            value = self._read(db, "operations", operation_id)
            if not value:
                raise StateError("Operation is not recorded")
            if value["phase"] == "completed":
                if value["result"] != result:
                    raise StateError("A completed receipt cannot be replaced")
                return
            if value["phase"] not in {"submitted", "unknown"}:
                raise StateError("Operation was not submitted or requires explicit recovery")
            if result.get("operation_id", operation_id) != operation_id:
                raise StateError("Result belongs to a different operation")
            value.update(
                phase="completed",
                result=result,
                error=None,
                may_have_effects=result.get("may_have_effects") is not False,
                updated_at=time.time(),
            )
            self._put_operation(db, value)
            self._touch(db)

    def failed(self, operation_id: str, message: str, *, outcome_unknown: bool, result: dict | None = None) -> None:
        if type(outcome_unknown) is not bool or not isinstance(message, str):
            raise StateError("Failure outcome is invalid")
        with self._transaction() as db:
            value = self._read(db, "operations", operation_id)
            if not value or value["phase"] == "completed":
                raise StateError("Cannot fail an absent or completed operation")
            effects = outcome_unknown or value["may_have_effects"]
            if result is not None:
                if not isinstance(result, dict) or result.get("operation_id") != operation_id:
                    raise StateError("Failure receipt must identify the exact operation")
                _json(result)
                no_effect = (
                    not outcome_unknown
                    and result.get("ok") is False
                    and result.get("phase") == "failed_no_effect"
                    and result.get("may_have_effects") is False
                    and result.get("executor_terminal") is True
                )
                if no_effect:
                    effects = False
                value["result"] = result
            value.update(
                phase="unknown" if outcome_unknown else "failed",
                may_have_effects=effects,
                error=message[:2000],
                updated_at=time.time(),
            )
            self._put_operation(db, value)
            claim = self._read(db, "claims", value["slot_id"])
            if claim and claim["operation_id"] == operation_id:
                self._put_claim(db, {**claim, "state": "unknown" if effects else "held"})
            self._touch(db)

    def put_instance(self, slot_id: str, value: dict) -> None:
        slot_id = _identifier(slot_id, "slot_id")
        if not isinstance(value, dict) or value.get("slot_id", slot_id) != slot_id:
            raise StateError("Instance identity does not match its slot")
        _json(value)
        with self._transaction() as db:
            claim = self._read(db, "claims", slot_id)
            if not claim or type(value.get("generation")) is not int or value["generation"] != claim["generation"]:
                raise StateError("Instance must match a recorded complete resource group")
            previous = self._read(db, "instances", slot_id)
            current = {**value, "slot_id": slot_id}
            current["container_ids"] = _container_ids(current)
            if (
                previous
                and claim["state"] != "released"
                and previous.get("generation") == current["generation"]
                and previous.get("container_ids")
                and previous["container_ids"] != current["container_ids"]
            ):
                raise StateError("Known instance container identities cannot be replaced within a generation")
            if previous == current:
                return
            db.execute(
                "INSERT INTO instances VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (slot_id, _json(current)),
            )
            if claim["state"] != "released" and current["container_ids"]:
                self._put_claim(db, {**claim, "state": "unknown" if current.get("status") == "unknown" else "active"})
            self._touch(db)

    def get_operation(self, operation_id: str) -> dict | None:
        operation_id = _identifier(operation_id, "operation_id")
        with self._mutex:
            db = self._connection()
            return self._read(db, "operations", operation_id) if db is not None else None

    def release(self, slot_id: str, proof: dict) -> None:
        if (
            not isinstance(proof, dict)
            or proof.get("confirmed") is not True
            or proof.get("executor_terminal") is not True
            or proof.get("may_have_effects") is not False
        ):
            raise StateError("Release requires confirmed terminal execution evidence")
        operation_id = _identifier(proof.get("operation_id"), "operation_id")
        with self._transaction() as db:
            claim = self._read(db, "claims", slot_id)
            if not claim:
                raise StateError("No resource group is recorded for this slot")
            operation = self._read(db, "operations", operation_id)
            if (
                not operation
                or operation["slot_id"] != slot_id
                or operation["generation"] != claim["generation"]
                or operation["operation_id"] != claim["operation_id"]
            ):
                raise StateError("Release proof does not match the current resource group")
            instance = self._read(db, "instances", slot_id) or {}
            if instance.get("generation") != claim["generation"]:
                instance = {}
            expected_ids = _container_ids(instance)
            status = proof.get("status")
            if status == "not_created":
                valid = (
                    operation["phase"] == "failed"
                    and not operation["may_have_effects"]
                    and operation["action"] in _CREATING
                    and not expected_ids
                    and not _container_ids(proof)
                    and (
                        operation.get("submitted_at") is None
                        or (operation.get("result") or {}).get("phase") == "failed_no_effect"
                    )
                )
            else:
                result = operation.get("result") or {}
                expected_status = {"stop": "stopped", "remove": "removed"}.get(operation["action"])
                valid = (
                    operation["phase"] == "completed"
                    and isinstance(status, str)
                    and status == expected_status
                    and result.get("phase", result.get("status")) == status
                    and result.get("may_have_effects") is False
                    and bool(expected_ids)
                    and _container_ids(proof) == expected_ids
                    and _container_ids(result) == expected_ids
                )
            if not valid:
                raise StateError("Unknown, partial or unrelated evidence cannot release resources")
            if claim["state"] != "released":
                self._put_claim(db, {**claim, "state": "released"})
                if instance:
                    instance.update(status=status, last_change=time.time())
                    db.execute("UPDATE instances SET body=? WHERE id=?", (_json(instance), slot_id))
                self._touch(db)

    def close(self) -> None:
        with self._mutex:
            if self._db is not None:
                self._db.close()
                self._db = None
