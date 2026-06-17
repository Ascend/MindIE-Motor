# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import threading
import time
from enum import Enum

from motor.common.resources.instance import Instance, PDRole
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.controller.core import InstanceManager

logger = get_logger(__name__)

NPU_ID = "NPUID"
NPU_IP = "NPUIP"
POD_ID = "podID"
POD_NAME = "podName"
POD_ASSOCIATED_INFO_LIST = "podAssociatedInfoList"
SERVER_IP_LIST = "serverIPList"
MODEL_ID = "sys_id"
SERVICE_ID = "SERVICE_ID"


class ModelState(Enum):
    HEALTHY = 1
    SUB_HEALTHY = 2
    UNHEALTHY = 3


class DPRole(Enum):
    CENTRAL = "Central"
    WORKER = "Worker"


class InstanceStatus(Enum):
    RUNNING = "running"
    ERROR = "error"
    INIT = "init"


def _get_service_created_time():
    service_id = os.getenv(SERVICE_ID, "")
    return service_id.rsplit("_", 1)[-1] if service_id else ""


def _get_backup_server_list():
    backup_info_list = {"backupInfoList": [{"backupRole": "", "serverIp": ""}]}
    return [backup_info_list]


def _get_expert_list():
    pod_info = {POD_ASSOCIATED_INFO_LIST: [{NPU_ID: "", NPU_IP: ""}], POD_ID: "", POD_NAME: ""}
    expert_info = {"DPIP": "", "ID": "", "Name": "", "podInfoList": [pod_info], "serverIP": ""}
    return [expert_info]


def _get_server_of_coordinator():
    return []


def _get_server_of_controller_slave():
    return []


def _get_server_of_controller_master():
    return []


def _get_instance_info_from_instance(instance: Instance, instance_status: InstanceStatus):
    server_ip_list = []

    pod_info_list = []
    for pod_ip, pod_info in instance.endpoints.items():
        server_ip_list.append(pod_ip)
        pod_associated_info_list = []
        for _, endpoint_info in pod_info.items():
            for device_info in endpoint_info.device_infos:
                new_pod_info = {NPU_ID: device_info.device_id, NPU_IP: device_info.device_ip}
                pod_associated_info_list.append(new_pod_info)

        new_pod_info = {POD_ASSOCIATED_INFO_LIST: pod_associated_info_list, POD_ID: pod_ip, POD_NAME: ""}
        pod_info_list.append(new_pod_info)

    instance_info = {
        "ID": instance.job_name,
        "Name": instance.job_name,
        "InstanceStatus": instance_status,
        "podInfoList": pod_info_list,
        SERVER_IP_LIST: list(set(server_ip_list)),
        "serverList": [],
    }
    return instance_info


class InventoryCollector(ThreadSafeSingleton):
    def __init__(self):
        self.lock = threading.Lock()
        self.active_instance_list = []
        self.inactive_instance_list = []
        self.initial_instance_list = []

    def collect_inventory(self) -> dict:
        self.active_instance_list = InstanceManager().get_active_instances()
        self.initial_instance_list = InstanceManager().get_initial_instances()
        self.inactive_instance_list = InstanceManager().get_inactive_instances()
        model_id = os.getenv(MODEL_ID, "")

        model_service_info = {
            "inventories": self._collect_inventory_detail(),
            "created": _get_service_created_time(),
            "inferenceFrameworkType": f"motor-{os.getenv('ENGINE_TYPE', '').lower()}",
            "modelID": model_id,
            "modelName": self._get_model_name(),
            "modelState": self._get_model_state(),
            "modelType": self._get_model_name(),
            "timestamp": int(time.time() * 1000),
        }

        return model_service_info

    def _collect_inventory_detail(self) -> dict:
        p_instance_list, d_instance_list, u_instance_list = self._get_instance_role_lists()
        all_server_ip_list = []
        for instance_info in p_instance_list:
            all_server_ip_list.extend(self._extract_server_ips(instance_info))
        for instance_info in d_instance_list:
            all_server_ip_list.extend(self._extract_server_ips(instance_info))
        for instance_info in u_instance_list:
            all_server_ip_list.extend(self._extract_server_ips(instance_info))

        inventory_detail = {
            "DInstanceList": d_instance_list,
            "PInstanceList": p_instance_list,
            "DPGroupList": self._get_dp_group_list(),
            "PDHybridList": u_instance_list,
            "backupServerList": _get_backup_server_list(),
            "expertList": _get_expert_list(),
            SERVER_IP_LIST: list(set(all_server_ip_list)),
            "serverOfCoordinator": _get_server_of_coordinator(),
            "serverOfManagerMaster": _get_server_of_controller_master(),
            "serverOfManagerSlave": _get_server_of_controller_slave(),
        }
        return inventory_detail

    @staticmethod
    def _extract_server_ips(instance_info: dict) -> list[str]:
        server_ips = instance_info.get(SERVER_IP_LIST) or []
        if server_ips:
            return server_ips
        return [pod_info.get(POD_ID) for pod_info in instance_info.get("podInfoList", []) if pod_info.get(POD_ID)]

    def _get_dp_group_list(self):
        dp_group_list = []
        instance_name_list = []
        for temp_instance in self.active_instance_list + self.initial_instance_list + self.inactive_instance_list:
            temp_instance_name = temp_instance.job_name
            if temp_instance_name in instance_name_list:
                continue
            instance_name_list.append(temp_instance_name)
            # every temp_instance equals to one instance(P or D)
            host_ip = ""
            for pod_ip, pod_info in temp_instance.endpoints.items():
                # every pod_info equals to one pod
                pod_npu_info_list = []
                temp_dp_group_list = []
                for endpoint_id, endpoint_info in pod_info.items():
                    # every endpoint_info equals to one dp
                    npu_info_list = []
                    for device_info in endpoint_info.device_infos:
                        npu_info = {NPU_ID: device_info.device_id, NPU_IP: device_info.device_ip}
                        npu_info_list.append(npu_info)
                    pod_npu_info_list.extend(npu_info_list)
                    server_info = {
                        "NPUInfoList": npu_info_list,
                        "serverID": host_ip,
                        "serverIP": pod_ip,
                        "serverName": "",
                    }
                    new_pod_info = {POD_ASSOCIATED_INFO_LIST: [], POD_ID: pod_ip, POD_NAME: ""}
                    dp_info = {
                        "DPID": endpoint_id,
                        "DPName": "",
                        "DPRole": DPRole.CENTRAL,
                        "PDInstID": temp_instance_name,
                        "podInfoList": [new_pod_info],
                        "serverList": [server_info],
                    }
                    dp_group = {"DPGroupID": endpoint_id, "DPGroupName": endpoint_id, "DPList": [dp_info]}
                    temp_dp_group_list.append(dp_group)
                for dp_group in temp_dp_group_list:
                    for dp_info in dp_group["DPList"]:
                        for new_pod_info in dp_info["podInfoList"]:
                            new_pod_info[POD_ASSOCIATED_INFO_LIST] = pod_npu_info_list

                dp_group_list.extend(temp_dp_group_list)
        return dp_group_list

    def _get_model_name(self):
        # if get model_name then return
        for temp_instance in self.active_instance_list:
            return temp_instance.model_name
        for temp_instance in self.inactive_instance_list:
            return temp_instance.model_name
        for temp_instance in self.initial_instance_list:
            return temp_instance.model_name
        logger.warning("Model name is not exist!")
        return ""

    def _get_model_state(self):
        prefill_count = 0
        decode_count = 0
        hybrid_count = 0
        instance_name_list = []
        for temp_instance in self.active_instance_list:
            if temp_instance.role == PDRole.ROLE_P:
                prefill_count += 1
            elif temp_instance.role == PDRole.ROLE_D:
                decode_count += 1
            elif temp_instance.role == PDRole.ROLE_U:
                hybrid_count += 1
            instance_name_list.append(temp_instance.job_name)

        if hybrid_count > 0:
            for temp_instance in self.initial_instance_list:
                temp_instance_name = temp_instance.job_name
                if temp_instance_name not in instance_name_list:
                    return ModelState.SUB_HEALTHY
            for temp_instance in self.inactive_instance_list:
                temp_instance_name = temp_instance.job_name
                if temp_instance_name not in instance_name_list:
                    return ModelState.SUB_HEALTHY
            return ModelState.HEALTHY

        if prefill_count == 0 or decode_count == 0:
            return ModelState.UNHEALTHY
        # check whether there are instances in initial_instance_list/inactive_instance_list
        for temp_instance in self.initial_instance_list:
            temp_instance_name = temp_instance.job_name
            if temp_instance_name not in instance_name_list:
                return ModelState.SUB_HEALTHY
        for temp_instance in self.inactive_instance_list:
            temp_instance_name = temp_instance.job_name
            if temp_instance_name not in instance_name_list:
                return ModelState.SUB_HEALTHY
        return ModelState.HEALTHY

    def _get_instance_role_lists(self):
        p_instance_list = []
        d_instance_list = []
        u_instance_list = []
        instance_name_list = []
        for temp_instance in self.active_instance_list:
            if temp_instance.role == PDRole.ROLE_P:
                p_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.RUNNING))
            elif temp_instance.role == PDRole.ROLE_D:
                d_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.RUNNING))
            elif temp_instance.role == PDRole.ROLE_U:
                u_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.RUNNING))
            instance_name_list.append(temp_instance.job_name)
        for temp_instance in self.initial_instance_list:
            temp_instance_name = temp_instance.job_name
            if temp_instance_name in instance_name_list:
                continue
            if temp_instance.role == PDRole.ROLE_P:
                p_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.INIT))
            elif temp_instance.role == PDRole.ROLE_D:
                d_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.INIT))
            elif temp_instance.role == PDRole.ROLE_U:
                u_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.INIT))
            instance_name_list.append(temp_instance_name)
        for temp_instance in self.inactive_instance_list:
            temp_instance_name = temp_instance.job_name
            if temp_instance_name in instance_name_list:
                continue
            if temp_instance.role == PDRole.ROLE_P:
                p_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.ERROR))
            elif temp_instance.role == PDRole.ROLE_D:
                d_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.ERROR))
            elif temp_instance.role == PDRole.ROLE_U:
                u_instance_list.append(_get_instance_info_from_instance(temp_instance, InstanceStatus.ERROR))
            instance_name_list.append(temp_instance_name)
        return p_instance_list, d_instance_list, u_instance_list
