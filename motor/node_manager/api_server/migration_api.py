# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import json
import os
import time
from contextlib import contextmanager

import requests
from fastapi import APIRouter, HTTPException, Request, status

from motor.common.logger import get_logger
from motor.node_manager.core.daemon import Daemon
from motor.node_manager.core.services.memcache.migration import DrainNotStarted

logger = get_logger(__name__)
router = APIRouter(prefix="/node-manager/migration")


def _get_memcache_migration_service():
    daemon = Daemon()
    service = daemon._services.get("kv_store")  # pylint: disable=protected-access
    migration = getattr(service, "migration", None)
    if migration is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="memcache migration is unavailable")
    return migration


def _migration_http_exception(error: Exception) -> HTTPException:
    """Preserve local availability and upstream client errors for UUID operations."""
    if isinstance(error, requests.HTTPError) and error.response is not None:
        response = error.response
        if 400 <= response.status_code < 500:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            return HTTPException(status_code=response.status_code, detail=detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error))


@contextmanager
def _memcache_operation():
    """Translate service lookup and Memcache errors at the migration API boundary."""
    try:
        yield _get_memcache_migration_service()
    except HTTPException:
        raise
    except Exception as error:
        logger.error("Memcache migration request failed: %s", error)
        raise _migration_http_exception(error) from error


@router.get("/inventory")
async def migration_inventory():
    with _memcache_operation() as service:
        return await asyncio.to_thread(service.drain_inventory)


@router.post("/trigger")
async def trigger_migration(request: Request):
    """Submit a real memcache drain job and return its UUID immediately."""
    try:
        payload = await request.json()
    except Exception as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body") from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object")
    targets = payload.get("target_node_labels")
    concurrency = payload.get("max_concurrency")
    if not isinstance(targets, list) or not targets or not all(isinstance(item, str) and item for item in targets):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_node_labels must be a non-empty list of strings",
        )
    if concurrency is not None and (
        isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency <= 0
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="max_concurrency must be positive")
    expected_uid = payload.get("source_uid")
    operation_id = payload.get("operation_id")
    deadline = None
    if "timeout_sec" in payload:
        timeout = payload["timeout_sec"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise HTTPException(status_code=400, detail="timeout_sec must be in (0, 600]")
        deadline = time.monotonic() + timeout
    if (
        not isinstance(expected_uid, str)
        or not expected_uid
        or not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id) > 160
    ):
        raise HTTPException(status_code=400, detail="Directed drain requires source_uid and operation_id")
    if expected_uid != os.environ.get("POD_UID"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="source Pod UID changed")
    with _memcache_operation() as service:
        return await asyncio.to_thread(
            service.submit_directed_drain,
            operation_id,
            expected_uid,
            targets,
            concurrency,
            deadline=deadline,
        )


@router.get("/status")
async def query_migration_status(drain_id: str):
    """Query a Memcache drain by UUID."""
    if not drain_id:
        raise HTTPException(status_code=400, detail="drain_id must be a non-empty string")
    with _memcache_operation() as service:
        return await asyncio.to_thread(service.query_drain, drain_id)


@router.post("/cancel")
async def cancel_migration(request: Request):
    """Request cancellation of a Memcache drain by UUID."""
    try:
        payload = await request.json()
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body") from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object")
    drain_id = payload.get("drain_id")
    if not isinstance(drain_id, str) or not drain_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="drain_id must be a non-empty string")
    with _memcache_operation() as service:
        return await asyncio.to_thread(service.cancel_drain, drain_id)


@router.post("/prepare-exit")
async def prepare_exit(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid provider exit JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a provider exit object")
    uid = body.get("source_uid")
    timeout = body.get("timeout", 30)
    drained = body.get("requests_drained", False)
    valid_timeout = type(timeout) in (int, float) and 1 <= timeout <= 600
    if (
        not isinstance(uid, str)
        or not valid_timeout
        or not isinstance(drained, bool)
        or body.get("scale_in", True) is not True
        or body.get("retiring_pod_ips", []) != []
    ):
        raise HTTPException(status_code=400, detail="Invalid provider exit request")
    if uid != os.environ.get("POD_UID") or not uid:
        raise HTTPException(status_code=409, detail="Source Pod UID mismatch")
    service = _get_memcache_migration_service()
    try:
        return await asyncio.to_thread(service.prepare_local_exit, uid, drained, timeout)
    except DrainNotStarted as exc:
        logger.warning("PreStop drain was not submitted for Pod %s: %s", uid, exc)
        raise HTTPException(
            status_code=409, detail={"code": "drain_not_started", "source_uid": uid, "reason": str(exc)}
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except (ValueError, RuntimeError, OSError) as exc:
        logger.warning("PreStop could not establish the KV drain result for Pod %s: %s", uid, exc)
        raise HTTPException(
            status_code=409, detail={"code": "drain_outcome_unknown", "source_uid": uid, "reason": str(exc)}
        ) from exc


async def require_source_uid(request: Request) -> None:
    try:
        body = await request.json()
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid stop request") from err
    source_uid = body.get("source_uid") if isinstance(body, dict) else None
    current_uid = os.getenv("POD_UID")
    if not isinstance(source_uid, str) or not source_uid.strip() or not current_uid or not current_uid.strip():
        raise HTTPException(status_code=409, detail="Stop Pod UID mismatch")
    if source_uid != current_uid:
        raise HTTPException(status_code=409, detail="Stop Pod UID mismatch", headers={"X-Motor-Pod-UID": current_uid})
