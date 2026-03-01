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

import json
import time
import os
from json import JSONDecodeError

from om_adapter.config import ConfigUtil
from om_adapter.thread_safe_util import ThreadSafeFactory
from om_adapter.common.util import safe_open
from .base_monitor import BaseMonitor, check_element

CATEGORY_STR = "category"
ALARM_ID_STR = "alarmId"
MODEL_ID_STR = "modelID"
INVENTORIES_STR = "inventories"
METRICS_STR = "metrics"


def response_raise_for_status(response, interface_name: str):
    if response.status >= 400:
        raise RuntimeError(f"Response from {interface_name} failed, status is {response.status}, "
                           f"content is {response.data.decode()}")


class CCAEMonitor(BaseMonitor):
    def __init__(self, backend_name: str, identity: str):
        super().__init__(backend_name, identity)
        # model_id_period 值为一个三元list
        # 第一位 bool 代表是否需要立即上报
        # 第二位 int 代表上报的时间间隔，以秒为单位
        # 第三位 float 代表上一次上报的时间戳，以秒为单位
        self.model_id_period = ThreadSafeFactory.make_threadsafe_instance(dict)
        self.alarm_cache = ThreadSafeFactory.make_threadsafe_instance(dict)
        for _ in range(1):
            model_id = os.getenv("MODEL_ID")
            if model_id is None:
                raise RuntimeError("Environment variable $MODEL_ID is not set.")
            max_env_len = 256
            if len(model_id) > max_env_len:
                raise RuntimeError("Environment variable $MODEL_ID is not correct.")
            self.model_id_period[model_id] = [False, 1, time.time()]
        self.version = self.fetch_version_info()
        self.component_type = -1
        if identity == "Coordinator":
            self.component_type = 1
        elif identity == "Controller":
            self.component_type = 0

    def fetch_version_info(self) -> str:
        server_dir = os.getenv('MIES_INSTALL_PATH')
        if server_dir is None:
            raise RuntimeError("Environment variable $MIES_INSTALL_PATH is not set.")
        with safe_open(os.path.join(server_dir, "version.info")) as f:
            for line in f:
                if "mindie-service" in line:
                    return line.split(":")[-1].strip()
        self.logger.error("Failed to fetch version info.")
        return "UNKNOWN VERSION"

    def send_heart_beat(self):
        url = f"{self.url_prefix}/rest/ccaeommgmt/v1/managers/mindie/register"
        request_data = {
            "timeStamp": int(time.time() * 1000),
            "modelServiceInfo": [],
            "componentType": self.component_type,
            "version": self.version,
        }
        for model_id, _ in self.model_id_period.items():
            request_data["modelServiceInfo"].append({
                MODEL_ID_STR: model_id,
                "modelName": ConfigUtil.get_config("model_type"),
            })
        try:
            response = self.http_pool_manager.request(
                "POST", url, headers=self.headers, body=json.dumps(request_data).encode())
            response_raise_for_status(response, "heartbeat")
            self.logger.debug("Response from heartbeat is: %s", response.data.decode())
        except Exception as e:
            self.heart_beat_flag = False
            self.logger.error(e)
            return
        response_json = json.loads(response.data.decode())
        if response_json["retCode"] != 0:
            raise RuntimeError(f"Failed to send heartbeat! Return message from ccae is: {response_json['retMsg']}")
        check_element(response_json, "reqList")
        for req in response_json["reqList"]:
            check_element(req, MODEL_ID_STR)
            model_id = req[MODEL_ID_STR]
            check_element(req, INVENTORIES_STR)
            check_element(req[INVENTORIES_STR], "forceUpdate")
            check_element(req, METRICS_STR)
            check_element(req[METRICS_STR], "metricPeriod")
            self.model_id_period[model_id][0] = req[INVENTORIES_STR]["forceUpdate"]
            self.model_id_period[model_id][1] = req[METRICS_STR]["metricPeriod"]
            self.log_topic = req["logsServer"]["topic"]
            self.log_ports = req["logsServer"]["servicePort"]
        self.heart_beat_flag = True

    def fetch_models_and_update(self) -> list:
        models_to_upload = []
        for model_id, send_tuple in self.model_id_period.items():
            if not send_tuple[0] and time.time() < send_tuple[1] + send_tuple[2]:
                continue
            self.model_id_period[model_id][0] = False
            self.model_id_period[model_id][2] = time.time()
            models_to_upload.append(model_id)
        return models_to_upload

    def upload_alarm(self, alarms: str) -> bool:
        try:
            alarm_list = json.loads(alarms)
        except JSONDecodeError as e:
            raise ValueError(f"Json decode error. Invalid alarm info read from backend sharememory: {alarms}") from e
        for item in alarm_list:
            if CATEGORY_STR not in item:
                raise ValueError(f"Failed to send alarms, lack key `{CATEGORY_STR}`")
            if ALARM_ID_STR not in item:
                raise ValueError("Failed to send alarms, lack key alarm_id")
            # a new alarm
            if item[CATEGORY_STR] == 1:
                self.alarm_cache[item[ALARM_ID_STR]] = item
            # cancel an alarm
            elif item[CATEGORY_STR] == 2:
                if item[ALARM_ID_STR] in self.alarm_cache.keys():
                    del self.alarm_cache[item[ALARM_ID_STR]]

        try:
            url = f"{self.url_prefix}/rest/ccaeommgmt/v1/managers/mindie/events"
            response = self.http_pool_manager.request(
                "POST", url, headers=self.headers, body=alarms.encode())
            response_raise_for_status(response, "alarm")
            self.logger.debug("Response from alarm is: %s", response.data.decode())
            return True
        except Exception as e:
            self.logger.error(e)
            return False

    def upload_inventory(self, inventories: str):
        try:
            inventory_json = json.loads(inventories)
            inventory_json["modelServiceInfo"][0]["timeStamp"] = int(time.time() * 1000)
            url = f"{self.url_prefix}/rest/ccaeommgmt/v1/managers/mindie/inventory"
            response = self.http_pool_manager.request(
                "POST", url, headers=self.headers, body=json.dumps(inventory_json).encode())
            response_raise_for_status(response, "inventory")
            self.logger.debug("Response from inventory is: %s", response.data.decode())
        except JSONDecodeError as json_error:
            self.logger.error(f"Failed to decode inventory info: {inventories}")
        except Exception as e:
            self.logger.error(e)

    def upload_log(self, log_request_message: dict):
        try:
            self.producer.send(self.log_topic, log_request_message)
        except Exception as e:
            self.logger.error(e)

    def fetch_alarm_cache(self) -> list:
        return list(self.alarm_cache.values())
