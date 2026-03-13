#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import socket
import traceback

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from ..core.service import Service
from ..models import NodeRunningStatus
from ..models.dataclass import ControllerCmd

RUNNING_STATUS_URL = '/v1/node-manager/running-status'
FAULT_HANDLING_COMMAND_URL = '/v1/node-manager/fault-handling-command'
HARDWARE_FAULT_INFO_URL = '/v1/node-manager/hardware-fault-info'
DEFAULT_CONTENT_KEY = 'Message'

router = APIRouter()


@router.get(RUNNING_STATUS_URL)
def running_status(request: Request):
    x_forwarded_for = request.headers.get("x-forwarded-for")
    ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else request.client.host
    local_addrs = ["127.0.0.1", "::1"]
    try:
        local_addrs += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)]
    except OSError:
        pass
    if ip not in local_addrs:
        Service.log_controller_ip(ip)

    node_running_status = Service.get_node_running_status()
    return JSONResponse(
        content={'status': node_running_status},
        status_code=200 if node_running_status != NodeRunningStatus.ABNORMAL.value else 210
    )


@router.post(FAULT_HANDLING_COMMAND_URL)
def fault_handling_command(json_data: dict):
    try:
        parsed_fault_cmd_info = Service.parse_fault_cmd_info(json_data)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            content={DEFAULT_CONTENT_KEY: f"Fail to parse fault cmd info: {e}"},
            status_code=400
        )

    cmd = parsed_fault_cmd_info['cmd']
    if cmd == ControllerCmd.STOP_ENGINE.value:
        Service.stop_node_server()
        return JSONResponse(content={}, status_code=200)

    handle_result = Service.fault_handle(cmd)
    if not handle_result.get('status'):
        return JSONResponse(
            content={
                DEFAULT_CONTENT_KEY: f"Fail to exec {parsed_fault_cmd_info['cmd']}: {handle_result.get('reason')}"},
            status_code=400
        )

    return JSONResponse(content={}, status_code=200)


@router.post(HARDWARE_FAULT_INFO_URL)
def hardware_fault_info(json_data: dict):
    try:
        Service.parse_hardware_fault_info(json_data)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            content={DEFAULT_CONTENT_KEY: f"Fail to parse hardware fault info: {e}"},
            status_code=400
        )

    return JSONResponse(content={}, status_code=200)
