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

from typing import List, Dict, Any
import threading
import concurrent.futures

from node_manager.core.heartbeat_mng import heartbeat_mng
from node_manager.framework.client import Client
from node_manager.common.logging import Log
from node_manager.models.enums import (
    EngineCmd,
    NodeRunningStatus,
    ServiceStatus,
    ControllerCmd,
)
from node_manager.common.utils import _SingletonMeta
from node_manager.core.config import GeneralConfig

STATUS_STR = "status"
REASON_STR = "reason"
DATA_STR = "data"


class FaultManager(metaclass=_SingletonMeta):

    def __init__(self, retry_times=5, timeout=90):
        self.logger = Log(__name__).getlog()

        self.fault_handle_map = {
            "PAUSE_ENGINE": self._pause_engine,
            "PAUSE_ENGINE_ROCE": lambda: self._pause_engine(roce=True),
            "REINIT_NPU": self._reinit_npu,
            "START_ENGINE": self._start_engine,
        }
        # PAUSE_ENGINE 与 PAUSE_ENGINE_ROCE 状态转移一致，共用同一表避免不同步
        _pause_transitions = [
            [NodeRunningStatus.NORMAL.value, NodeRunningStatus.PAUSE.value],
            [NodeRunningStatus.ABNORMAL.value, NodeRunningStatus.PAUSE.value],
            [NodeRunningStatus.BUSY.value, NodeRunningStatus.PAUSE.value],
        ]
        self.cmd_state_transition_table = {
            # key:cmd, value:[[cmd执行之前的状态,cmd执行之后的状态]]
            "PAUSE_ENGINE": _pause_transitions,
            "PAUSE_ENGINE_ROCE": _pause_transitions,
            "REINIT_NPU": [
                [NodeRunningStatus.PAUSE.value, NodeRunningStatus.READY.value],
            ],
            "START_ENGINE": [
                [NodeRunningStatus.READY.value, NodeRunningStatus.NORMAL.value],
                [NodeRunningStatus.PAUSE.value, NodeRunningStatus.NORMAL.value]
            ],
        }
        self.last_cmd_rpl = True  # 上一条cmd命令是否收到回复,只有TRUE才能下发新命令

        self._lock = threading.Lock()
        self.heartbeat_mng = None
        self.client = Client(retry_times, timeout)
        self.logger = Log(__name__).getlog()

    def get_handler(self, cmd: str):
        return self.fault_handle_map.get(cmd)

    def init_heartbeat_mng(self) -> None:
        if self.heartbeat_mng is None:
            self.heartbeat_mng = heartbeat_mng

    def _find_matching_index(self, cmd: str, target_defore_status: str):
        
        transitions = self.cmd_state_transition_table.get(cmd, [])
        for i, (before, after) in enumerate(transitions):
            if before == target_defore_status:
                return i, after
        self.logger.error(
            f"Can't find match before state in cmd_state_transition_table, \
            check if an incorrect cmd={cmd} was issued, current state={target_defore_status}."
        )
        return -1, None

    def _parse_cmd_to_client(self, cmd: int):
        self.logger.info(f"fault_manager:_parse_cmd_to_client cmd:{EngineCmd(cmd)}")
        ret_info_all = []
        server_engine_cnt = GeneralConfig().server_engine_cnt

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=server_engine_cnt
        ) as executor:
            future_to_idx = {
                executor.submit(
                    self.client.send_cmd_to_engine, thread_idx, cmd
                ): thread_idx
                for thread_idx in range(server_engine_cnt)
            }
            ret_info_all = [None] * server_engine_cnt
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                ret_info_all[idx] = future.result()
        # ret_info_all格式是
        # [{"success": bool(请求是否发送成功),  "data": dict(请求发送成功时，请求返回的数据), "msg": str(请求失败时，错误信息)},{...}]
        # data内含{"status":true/false, "reason":null}
        return ret_info_all

    def _extract_info(
        self, ret_info_all: List[Dict[str, Any]], field: str
    ) -> List[Any]:
        """
        根据指定字段名，从 ret_info_all 列表中的每个元素提取对应字段的值。
        若对应字段不存在，则返回 None。

        参数:
            ret_info_all (List[Dict[str, Any]]):
                请求结果列表。每个元素为字典，格式如下：
                {
                    "success": bool,           # 请求是否发送成功
                    "data": dict,              # 请求发送成功时返回的数据，如 {"status": true/false, "reason": null}
                    "msg": str                 # 请求失败时的错误信息
                }
            field (str):
                需要提取的字段名，可以为顶层字段或 data 内部字段。

        返回:
            List[Any]:
                包含每个元素对应字段的值的列表。如果字段不存在则为 None。
        """
        self.logger.info(f"[FaultManager] ret_info_all={ret_info_all}")
        result = []
        for item in ret_info_all:
            if field in item:
                result.append(item.get(field, None))
            elif DATA_STR in item and isinstance(item[DATA_STR], dict):
                result.append(item[DATA_STR].get(field, None))
            else:
                result.append(None)
        return result


    def _cmd_failed_further_action(self):
        """
        Trigger further actions based on the reason for command execution failure.

        Args:
            reason (str, optional): The reason why the command was not executed successfully.

        Function:
            This method sets the node's running status to 'abnormal'
            in response to unsuccessful command execution. It can be extended to include additional recovery or
            alert mechanisms based on the provided reason.
        """
        self.init_heartbeat_mng()
        self.logger.info(
            f"[_cmd_failed_further_action] Set running status = ABNORMAL"
        )
        self.heartbeat_mng.set_running_status(NodeRunningStatus.ABNORMAL.value)

    def _set_heartbeat_check_allowed(self, next_state: bool):
        with self._lock:
            if not self.heartbeat_mng:
                self.init_heartbeat_mng()
            self.heartbeat_mng.set_heartbeat_check_allowed(next_state)
        self.logger.info(f"Set heartbeat_check_allowed={next_state}")

    def _pause_engine(self, roce: bool = False) -> dict:
        """
        Sends a pause engine cmd to the engine server and returns the execution status and reason.

        Args:
            cmd: The command to be sent, can be a string or other type.
            should_delay: Whether to delay the execution of the command.
            roce: Whether to execute the command for RoCE recovery.

        Returns:
            dict: Contains the following keys:
                - status (bool): Whether the command was executed successfully.
                - reason (str): Explanation of why the command succeeded or failed.
        """
        self.logger.info(f"FaultManager: pause engine starts.")
        self.init_heartbeat_mng()
        # 状态表用 ControllerCmd 字符串，下发给引擎用 EngineCmd 枚举，按 roce 一次分支
        if roce:
            pause_cmd = ControllerCmd.PAUSE_ENGINE_ROCE.value
            pause_engine_cmd = EngineCmd.PAUSE_ENGINE_ROCE.value
        else:
            pause_cmd = ControllerCmd.PAUSE_ENGINE.value
            pause_engine_cmd = EngineCmd.PAUSE_ENGINE.value
        _, after_state = self._find_matching_index(
            cmd=pause_cmd,
            target_defore_status=self.heartbeat_mng.get_running_status(),
        )
        if after_state is not None:
            self.heartbeat_mng.set_running_status(after_state)
            self._set_heartbeat_check_allowed(False)

            ret_info_all = self._parse_cmd_to_client(cmd=pause_engine_cmd)
            ret_success = self._extract_info(ret_info_all, field="success")
            if all(ret_success):
                # 所有请求发送成功
                ret_data = self._extract_info(ret_info_all, field=STATUS_STR)
                if all(ret_data):
                    self.logger.info(
                        f"FaultManager: Successfully executed the PAUSE ENGINE cmd."
                    )
                    # 发送请求成功,命令执行成功
                    return {STATUS_STR: True, REASON_STR: None}
                else:
                    # 请求发送成功,命令执行有失败
                    self.logger.error(
                        f"FaultManager: PAUSE ENGINE cmd sent successfully, but the cmd execution failed."
                    )
                    self._set_heartbeat_check_allowed(
                        True
                    )  # 默认下一步指令可能不发送cmd,启动心跳检测,kill异常pod
                    reasons = self._extract_info(ret_info_all, field=REASON_STR)
                    reason_val_str = ",".join(
                        f"{idx}:{rsn if rsn else 'none'}"
                        for idx, rsn in enumerate(reasons)
                    )
                    self._cmd_failed_further_action()
                    return {STATUS_STR: False, REASON_STR: reason_val_str}
            else:
                # 请求发送失败
                # status表明当前命令的执行状态是否成功
                self.logger.error(f"FaultManager: Sending PAUSE ENGINE cmd failed.")
                self._set_heartbeat_check_allowed(
                    True
                )  # 默认下一步指令可能不发送cmd,启动心跳检测,kill异常pod
                msgs = self._extract_info(ret_info_all, field="msg")
                msg_str = ",".join(
                    f"{idx}:{msg if msg else 'none'}" for idx, msg in enumerate(msgs)
                )
                self._cmd_failed_further_action()
                return {STATUS_STR: False, REASON_STR: msg_str}
        else:
            return {
                STATUS_STR: False,
                REASON_STR: "The previous CMD has not finished executing yet.",
            }

    def _reinit_npu(self) -> dict:
        self.logger.info(f"FaultManager: REINIT NPU starts.")
        self.init_heartbeat_mng()
        _, after_state = self._find_matching_index(
            cmd=ControllerCmd.REINIT_NPU.value,
            target_defore_status=self.heartbeat_mng.get_running_status(),
        )
        if after_state is not None:
            get_ep_status_thread = threading.Thread(
                target=self._reinit_npu_async_execute,
                args=(after_state,),
                daemon=True
            )
            get_ep_status_thread.start()
            # 立即返回状态成功，实际结果稍后通过self._reinit_npu_async_execute更新到heartbeatMng状态
            return {STATUS_STR: True, REASON_STR: None}
        else:
            # 不在pause_engine->reinit_npu->start_engine指令流中
            self.logger.error(
                f"not supported single cmd, must in pause_engine->reinit_npu->start_engine cmd stream"
            )
            return {
                STATUS_STR: False,
                REASON_STR: "not supported single cmd, must in pause_engine->reinit_npu->start_engine cmd stream.",
            }
        
    def _reinit_npu_async_execute(self, after_state):
        ret_info_all = self._parse_cmd_to_client(cmd=EngineCmd.REINIT_NPU.value)
        ret_success = self._extract_info(ret_info_all, field="success")
        if all(ret_success):
            # 所有请求发送成功
            ret_data = self._extract_info(ret_info_all, field=STATUS_STR)
            if all(ret_data):
                # 发送请求成功,命令执行成功
                self.logger.info(
                    f"FaultManager: Successfully executed the REINIT NPU cmd."
                )
                self.heartbeat_mng.set_running_status(after_state)
            else:
                # 发送请求成功,命令执行有失败
                self.logger.error(
                    f"FaultManager: REINIT NPU cmd sent successfully, but the cmd execution failed."
                )
                self._set_heartbeat_check_allowed(
                    True
                )  # 默认下一步指令可能不发送cmd,启动心跳检测,kill异常pod
                reasons = self._extract_info(ret_info_all, field=REASON_STR)
                reason_val_str = ",".join(
                    f"{idx}:{rsn if rsn else 'none'}"
                    for idx, rsn in enumerate(reasons)
                )
                self._cmd_failed_further_action()
                self.logger.error(
                    f"FaultManager: REINIT NPU cmd execution failed, reason={reason_val_str}"
                )
        else:
            # 请求发送失败
            self._set_heartbeat_check_allowed(
                True
            )  # 默认下一步指令可能不发送cmd,启动心跳检测,kill异常pod
            msgs = self._extract_info(ret_info_all, field="msg")
            msg_str = ",".join(
                f"{idx}:{msg if msg else 'none'}" for idx, msg in enumerate(msgs)
            )
            self._cmd_failed_further_action()
            self.logger.error(f"FaultManager: Sending REINIT NPU cmd failed, reason={msg_str}.")



    def _start_engine(self):
        self.logger.info(f"FaultManager: start engine starts.")
        self.init_heartbeat_mng()
        _, after_state = self._find_matching_index(
            cmd=ControllerCmd.START_ENGINE.value,
            target_defore_status=self.heartbeat_mng.get_running_status(),
        )
        if after_state is not None:
            ret_info_all = self._parse_cmd_to_client(cmd=EngineCmd.START_ENGINE.value)
            ret_success = self._extract_info(ret_info_all, field="success")
            if all(ret_success):
                # 所有请求发送成功
                ret_data = self._extract_info(ret_info_all, field=STATUS_STR)
                if all(ret_data):
                    # 发送请求成功,命令执行成功
                    self.logger.info(
                        f"FaultManager: Successfully executed the START ENGINE cmd."
                    )
                    self._set_heartbeat_check_allowed(True)
                    self.heartbeat_mng.set_running_status(after_state)
                    return {STATUS_STR: True, REASON_STR: None}
                else:
                    # 发送请求成功,命令执行有失败
                    self.logger.warning(
                        f"FaultManager: START ENGINE cmd sent successfully, but the cmd execution failed."
                    )
                    self._set_heartbeat_check_allowed(
                        True
                    )  # 默认下一步指令可能不发送cmd,启动心跳检测,kill异常pod
                    reasons = self._extract_info(ret_info_all, field=REASON_STR)
                    reason_val_str = ",".join(
                        f"{idx}:{rsn if rsn else 'none'}"
                        for idx, rsn in enumerate(reasons)
                    )
                    self._cmd_failed_further_action()
                    return {STATUS_STR: False, REASON_STR: reason_val_str}
            else:
                # 请求发送失败
                self.logger.warning(f"FaultManager: Sending START ENGINE cmd failed.")
                self._set_heartbeat_check_allowed(
                    True
                )  # 默认server不下发下一步指令,启动心跳检测,kill异常pod
                msgs = self._extract_info(ret_info_all, field="msg")
                msg_str = ",".join(
                    f"{idx}:{msg if msg else 'none'}" for idx, msg in enumerate(msgs)
                )
                self._cmd_failed_further_action()
                return {STATUS_STR: False, REASON_STR: msg_str}
        else:
            return {
                STATUS_STR: False,
                REASON_STR: "The previous CMD has not finished executing yet.",
            }


fault_manager = FaultManager()
