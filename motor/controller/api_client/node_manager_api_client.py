# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from enum import Enum
from typing import Any

import requests

from motor.common.resources import NodeManagerInfo, StartCmdMsg
from motor.common.http.http_client import SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.config.controller import ControllerConfig

logger = get_logger(__name__)


class NodeManagerStopStatus(str, Enum):
    """Outcome of POST ``/node-manager/stop``."""

    OK = "ok"
    UNREACHABLE = "unreachable"
    FAILED = "failed"


def _is_node_manager_unreachable(exc: BaseException) -> bool:
    """True when stop/start failed because the NodeManager is already gone."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("timed out", "connection refused", "failed to establish a new connection"))


class NodeManagerApiClient:
    tls_config = ControllerConfig.from_json().mgmt_tls_config

    @staticmethod
    def send_start_command(node_mgr: NodeManagerInfo, start_cmd_msg: StartCmdMsg) -> bool:
        is_succeed = True
        client = None
        try:
            # For `superpod_id` we need to use `exclude_none` to avoid error,
            # when we use atlas A2 server which doesn't have superpod_id.
            client_args = NodeManagerApiClient._generate_client_args(node_mgr)
            client = SafeHTTPSClient(**client_args)
            client.post(
                "/node-manager/start",
                data=start_cmd_msg.model_dump(exclude_none=True),
            )
            logger.info(
                "Start command sent to node manager %s for instance %s successfully.",
                client_args.get('address', 'unknown'),
                start_cmd_msg.job_name,
            )
        except Exception as e:
            is_succeed = False
            logger.error(
                "Error sending start command to node manager %s for instance %s: %s",
                client_args.get('address', 'unknown'),
                start_cmd_msg.job_name,
                e,
            )
        finally:
            if client is not None:
                client.close()

        return is_succeed

    @staticmethod
    def stop(node_mgr: NodeManagerInfo) -> bool:
        return NodeManagerApiClient.stop_status(node_mgr) == NodeManagerStopStatus.OK

    @staticmethod
    def stop_status(node_mgr: NodeManagerInfo) -> NodeManagerStopStatus:
        """POST ``/node-manager/stop`` and distinguish unreachable from other errors."""
        addr = format_address(node_mgr.pod_ip, node_mgr.port)
        client = None
        try:
            client_args = NodeManagerApiClient._generate_client_args(node_mgr)
            client = SafeHTTPSClient(**client_args)
            client.post("/node-manager/stop", data={})
            logger.info("Stop command sent to node manager %s", addr)
            return NodeManagerStopStatus.OK
        except Exception as e:
            if _is_node_manager_unreachable(e):
                logger.warning(
                    "NodeManager already unreachable for stop, treating as exiting. address=%s, error=%s",
                    addr,
                    e,
                )
                return NodeManagerStopStatus.UNREACHABLE
            logger.error("Error sending stop command to node manager %s: %s", addr, e)
            return NodeManagerStopStatus.FAILED
        finally:
            if client is not None:
                client.close()

    @staticmethod
    def restart_engine(node_mgr: NodeManagerInfo, action: str = "restart", instance_id: int | None = None) -> bool:
        """Dispatch the engine relaunch (or its abort) to a NodeManager.

        ``action="restart"``: kill and re-pull all engines without restarting
        the container. ``action="abort"``: unfreeze the NodeManager's suicide
        counter so the heartbeat mechanism restarts the container (fallback
        when relaunch failed). Returns False when the NodeManager is
        unreachable or rejects the request.

        A 409 (relaunch already in progress on that NodeManager) counts as
        success: the work is being done there — treating it as a dispatch
        failure would retry and then wrongly escalate to container restart
        while the engines are actually being relaunched.
        """
        is_succeed = True
        addr = format_address(node_mgr.pod_ip, node_mgr.port)
        data = {"action": action}
        if instance_id is not None:
            data["instance_id"] = instance_id
        client = None
        try:
            client_args = NodeManagerApiClient._generate_client_args(node_mgr)
            client = SafeHTTPSClient(**client_args)
            client.post("/node-manager/engine-restart", data=data)
            logger.info("Engine restart command (%s) sent to node manager %s", action, addr)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                logger.info(
                    "Engine relaunch already in progress on node manager %s (409), treating as dispatched", addr
                )
            else:
                is_succeed = False
                logger.error("Error sending engine restart command (%s) to node manager %s: %s", action, addr, e)
        except Exception as e:
            is_succeed = False
            logger.error("Error sending engine restart command (%s) to node manager %s: %s", action, addr, e)
        finally:
            if client is not None:
                client.close()

        return is_succeed

    @classmethod
    def query_status(cls, node_mgr: NodeManagerInfo, relaxed: bool = False) -> dict[str, Any]:
        """Query the NodeManager's endpoint readiness.

        ``relaxed=True`` (engine-relaunch flow): True when no endpoint is
        ABNORMAL — a freshly relaunched engine is INITIAL (model loading) and
        counts as recovering; the strict mode requires all NORMAL.
        """
        client_args = NodeManagerApiClient._generate_client_args(node_mgr)
        client = SafeHTTPSClient(**client_args)
        params = {"relaxed": "true"} if relaxed else None
        response = client.get("/node-manager/status", params=params)
        return response

    @classmethod
    def _generate_client_args(cls, node_mgr: NodeManagerInfo) -> dict[str, str]:
        client_ars = {
            "address": format_address(node_mgr.pod_ip, node_mgr.port),
            "tls_config": cls.tls_config,
        }
        return client_ars
