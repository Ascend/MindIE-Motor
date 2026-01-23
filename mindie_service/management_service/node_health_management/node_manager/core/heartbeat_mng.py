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

import subprocess
import threading
from collections import deque
from datetime import datetime
import threading
import time
from typing import List, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from node_manager.models import NodeRunningStatus, ControllerReply
from node_manager.framework.client import Client
from node_manager.models.enums import ServiceStatus
from node_manager.core.config import GeneralConfig
from node_manager.common.utils import _SingletonMeta
from node_manager.common.logging import Log
from node_manager.daemon_manager.llm_daemon_starter import llm_daemon_manager

DATA_STR = "data"


class NodeStatusMonitor(metaclass=_SingletonMeta):

    def __init__(self, hbm, eng_st: deque):
        self.query_interval = GeneralConfig().heartbeat_interval
        self.engine_state = eng_st  # 心跳,若engine数量为2,格式为['<时间>','engine server1状态','engine server2状态']
        self.running = False
        self.heartbeat_mng = hbm
        self.client = None
        self.logger = Log(__name__).getlog()
        if not self.client:
            self.client = Client()
        self.executor = ThreadPoolExecutor(
            max_workers=GeneralConfig().server_engine_cnt
        )
        
    @staticmethod
    def _extract_error_data_to_ctrl(res_all):
        data_lst = []
        for res in res_all:
            tmp_dict = res.get("data", {}).get("error", {})
            if tmp_dict != {}:
                data_lst.append(tmp_dict)
        return data_lst

    @staticmethod
    def _is_child_process_detected() -> bool:
        process_infos = subprocess.run(
            ["/bin/ps", "-efH"],
            capture_output=True,
            text=True,
            check=True
        ).stdout
        return "mindieservice_daemon" in process_infos

    def start_monitoring(self):
        self.running = True
        if not self.client:
            self.client = Client()
        self._monitor_state()

    def stop_monitoring(self):
        self.running = False
        self.executor.shutdown()

    def _query_ep_status(self) -> Tuple[List[Any], List[Any]]:
        """使用线程池查询node状态"""
        result_dict = {}
        future_to_idx = {
            self.executor.submit(self.client.get_ep_status, idx): idx
            for idx in range(GeneralConfig().server_engine_cnt)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            result = future.result()
            result_dict[idx] = result
        result_all = [result_dict[idx] for idx in range(GeneralConfig().server_engine_cnt)]
        return result_all

    def _parse_engine_state(self) -> tuple[bool, bool, str]:
        # 在当前不执行指令时候才解析, 不会出现server engine状态不一致的问题
        engine_state_cur = self.engine_state[-1] # 首字段engine_state_cur[0]是时间戳
        if ServiceStatus.SERVICE_READY.value in engine_state_cur[1:]:
            self.logger.error(f"[parse_engine_state] Contain SERVICE_READY in engine state while no CMD executing")
            return NodeRunningStatus.ABNORMAL.value
        if ServiceStatus.SERVICE_PAUSE.value in engine_state_cur[1:]:
            self.logger.error(f"[parse_engine_state] Contain SERVICE_PAUSE in engine state while no CMD executing")
            return NodeRunningStatus.ABNORMAL.value
        if ServiceStatus.SERVICE_ABNORMAL.value in engine_state_cur[1:]:
            return NodeRunningStatus.ABNORMAL.value
        # 只剩init 和 normal
        if ServiceStatus.SERVICE_INIT.value in engine_state_cur[1:]:
            return NodeRunningStatus.INIT.value
        all_normal = all(
            s == ServiceStatus.SERVICE_NORMAL.value for s in engine_state_cur[1:]
        )
        if all_normal:
            return NodeRunningStatus.NORMAL.value
        else:
            self.logger.error(
                f"Unrecognizable server engine state={engine_state_cur}"
            )
            return NodeRunningStatus.ABNORMAL.value

    def _monitor_state(self):
        while self.running:
            self.logger.info(f"Monitering EP status")
            result_all = self._query_ep_status()
            if not self.heartbeat_mng.heartbeat_check_allowed:
                # CMD在运行或者heartbeatmng在处理异常,不做状态更新和处理
                self.logger.info(f"while handling cmd, not update heartbeat")
                time.sleep(self.query_interval)
                continue
            if self.heartbeat_mng.get_running_status() == NodeRunningStatus.PAUSE.value:  # 在处理异常
                self.logger.error(f"heartBeatMng is Paused while no cmd is executing")
                time.sleep(self.query_interval)
                continue
            now = datetime.utcnow().strftime("%Y/%m/%d %H:%M:%S")
            cur_success = [res.get("success", None) for res in result_all]
            if False in cur_success or None in cur_success:  # 查询ep status请求发送失败,此时不向ctrler上报异常
                cur_msg = ""
                for res in result_all:
                    cur_msg = cur_msg + res.get("msg", "")
                self.logger.warning(f"get_ep_state fails, reason: request send fails,msg={cur_msg}")
                time.sleep(self.query_interval)
                continue
            # 请求发送成功
            cur_state = [int(res.get(DATA_STR, {}).get("status", "")) for res in result_all]
            self.engine_state.append([now] + cur_state)# 更新heartbeat_mng的engine_state
            node_running_status = self._parse_engine_state()
            self.heartbeat_mng.set_running_status(node_running_status)
            if (node_running_status == NodeRunningStatus.ABNORMAL.value): # abnormal情况下发异常给ctrler 并自杀
                if self.heartbeat_mng.heartbeat_check_allowed:
                    self.logger.error("Detected ABNORMAL status while heartbeat_check_allowed is True.")
                    while self._is_child_process_detected():
                        self.logger.info("Daemon process detected, waiting for it to terminate")
                        time.sleep(1)
                    self.logger.error(f"Start to Terminating all processes")
                    llm_daemon_manager.terminate_all_processes()
                data_lst = self._extract_error_data_to_ctrl(result_all)
                ctl_rpl = self._send_ep_state_info_to_ctrler(data_lst)
                if ctl_rpl.get(DATA_STR, None) == ControllerReply.SEND_COORDINATOR_ALARM_SUCCESS.value:
                    self.logger.info(f"controller has successfully send alarm to coordinator")
                elif ctl_rpl.get(DATA_STR, None) == ControllerReply.SEND_COORDINATOR_ALARM_UNREACHEABLE.value:
                    self.logger.info(f"controller sending alarm to coordinator failed, service unreachable")
                else:
                    self.logger.error(f"controller reply not found,ctrl_rpl={ctl_rpl}")
            time.sleep(self.query_interval)
            
    def _send_ep_state_info_to_ctrler(self, error_info):
        """
        向controller发送ep的非健康状态(如果查询状态信息失败,不向controller上报)

        参数:
        error_info(list): 包含controller的ep不健康状态信息。
                        示例:
                        [
                            [
                                {
                                    "timestamp": 1231234123,
                                    "srrCode": "sss",
                                    "createBy": "engine",
                                    "addition": {
                                        "device_ip": "10.0.2.81",
                                        "device_id": 0,
                                    }
                                }
                            ]
                            ,
                            [....]
                        ]
        返回:
        dict: 包含请求是否发送成功、请求返回的数据和错误信息。
            示例:
            {
                "success": bool,  // 请求是否发送成功
                "data": dict,     // 请求发送成功时，请求返回的数据
                "msg": str        // 请求失败时，错误信息
            }
        """
        if not self.client:
            self.client = Client()
        return self.client.send_ctrler_error_info(error_info)


class HeartBeatMng(metaclass=_SingletonMeta):
    _initialized = False

    def __init__(self, running_status=None):
        if self._initialized:
            return
        self._running_status = running_status
        self._lock = threading.Lock()
        self.query_interval = GeneralConfig().heartbeat_interval  # 轮训间隔时间,单位s
        self.mem_window_len = 1  # server engine状态保存的时间窗长度
        # 心跳状态
        # 若engine数量为2,格式为['<时间>',server1状态,server2状态],状态值是int,相关枚举值是ServiceStatus
        self.engine_state = deque(maxlen=self.mem_window_len)
        self.heartbeat_check_allowed = True
        self._status_monitor = NodeStatusMonitor(self, eng_st=self.engine_state)
        self._initialized = True
        self.logger = Log(__name__).getlog()

    def run(self):
        if not GeneralConfig().has_endpoint:
            self.logger.info(f"Heartbeat Manager: No endpoint configured, skipping monitoring.")
            return
        self.logger.info(f"Heartbeat Manager:Start monitoring engine state.")
        self._status_monitor.start_monitoring()

    def stop(self):
        self._status_monitor.stop_monitoring()
        self.logger.info(f"Heartbeat Manager: Monitoring engine state stopped.")

    def get_heartbeat_check_allowed(self) -> bool:
        return self.heartbeat_check_allowed

    def set_heartbeat_check_allowed(self, hb_check_allowed: bool) -> bool:
        with self._lock:
            self.heartbeat_check_allowed = hb_check_allowed

    def get_running_status(self) -> str:
        return self._running_status

    def set_running_status(self, running_status: str):
        self.logger.info(
            f"[HeartBeatMng] set running status={NodeRunningStatus(running_status)}"
        )
        with self._lock:
            if running_status in [status.value for status in NodeRunningStatus]:
                self._running_status = running_status
            else:
                raise ValueError("unknown running status")


heartbeat_mng = HeartBeatMng()
