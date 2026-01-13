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
import logging
from dataclasses import dataclass
from itertools import accumulate
from typing import List, Dict, Tuple, Union

import psutil
import os
import argparse


class ENV:
    os.getenv("ASCEND_RT_VISIBLE_DEVICES", None)


logging.basicConfig(
    level=logging.INFO,
    format=(
        f'[%(asctime)s:%(msecs)04d+08:00] '
        f'[%(process)d] [{os.getppid()}] '
        f'[%(levelname)s] [%(filename)s:%(lineno)d] : %(message)s'
    ),
    datefmt='%Y-%m-%d %H:%M:%S'
)


def execute_command(cmd_list):
    with subprocess.Popen(cmd_list,
                          shell=False,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as p:
        out, err = p.communicate(timeout=1000)
    if err:
        error_msg = err.decode()
        logging.warning(f"Command execution warning: {error_msg}")
    res = out.decode()
    return res


@dataclass
class DeviceInfo:
    _info_line: str = ""
    npu_id: int = 0
    chip_id: int = 0
    chip_logic_id: Union[int, str] = 0
    chip_name: str = ""

    def __post_init__(self):
        self.npu_id, self.chip_id, self.chip_logic_id, self.chip_name = self._info_line.strip().split(None, 3)
        self.npu_id = int(self.npu_id)
        self.chip_id = int(self.chip_id)
        if self.chip_logic_id.isnumeric():
            self.chip_logic_id = int(self.chip_logic_id)


def _get_device_map_info() -> Dict[int, DeviceInfo]:
    device_map_info = {}
    device_map = execute_command(["npu-smi", "info", "-m"]).strip().split("\n")[1:]
    for line in device_map:
        device_info = DeviceInfo(line.strip())
        if isinstance(device_info.chip_logic_id, int):
            device_map_info[device_info.chip_logic_id] = device_info
    return device_map_info



def _get_pcie_info(devices: List[int], keyword="PCIeBusInfo"):
    device_map_info = _get_device_map_info()
    device_pcie_tbl = {}
    for device in devices:
        device_info = device_map_info.get(device)
        if not device_info:
            raise RuntimeError("Can not get device info, you can use BIND_CPU=0 to skip.")
        pcie_info = execute_command(["npu-smi", "info", "-t", "board", "-i", f"{device_info.npu_id}",
                                     "-c", f"{device_info.chip_id}"]).strip().split("\n")
        for _ in pcie_info:
            line = ''.join(_.split())  # 不同硬件的输出格式不同（PCIe Bus Info 或 PCIeBusInfo），在此处做统一
            if line.startswith(keyword):
                device_pcie_tbl[device] = line[len(keyword) + 1:]
                break

    return device_pcie_tbl


def _get_numa_info(pcie_tbl, keyword="NUMAnode"):
    device_numa_tbl = dict()  # key is device id, value is numa id
    numa_devices_tbl = dict()  # key is numa id, value is device id list

    for device, pcie_no in pcie_tbl.items():
        numa_info = execute_command(["lspci", "-s", f"{pcie_no}", "-vvv"]).split("\n")
        for _ in numa_info:
            line = ''.join(_.split())
            if line.startswith(keyword):
                numa_id = int(line[len(keyword) + 1:])
                device_numa_tbl[device] = numa_id

                devices = numa_devices_tbl.get(numa_id, None)
                if devices is None:
                    numa_devices_tbl[numa_id] = list()

                numa_devices_tbl[numa_id].append(device)
                break

    return device_numa_tbl, numa_devices_tbl


def get_cores_a3(device_id, devices):
    if device_id < 1 or not devices:
        raise Exception("device id must > 0, length of devices must > 0, please check !")
    device_id = device_id - 1
    ncores = os.cpu_count()
    cores_per_process = ncores // len(devices)
    start_core = device_id * cores_per_process
    end_core = start_core + cores_per_process
    cores = list(range(start_core, end_core))
    cores = cores[:-2]
    out_str = ",".join([str(i) for i in cores])
    return out_str


def get_cores_a2(device_id, devices, odd_numa=False):
    device_bus_id_map = _get_pcie_info(devices)
    try:
        device_numa_tbl, _ = _get_numa_info(device_bus_id_map)
    except Exception as e:
        raise RuntimeError("get numa info failed, error: {}".format(e)) from e
    current_numa = device_numa_tbl[device_id - 1]
    ncores = os.cpu_count()
    if not devices:
        raise Exception("length of devices must > 0, please check !")
    cores_per_process = ncores // len(devices)
    cpu_groups = list(range(cores_per_process - 1))
    cpu_groups = [x + cores_per_process * 2 * (current_numa // 2) for x in cpu_groups]
    if odd_numa:
        out_str = ",".join([str(i + cores_per_process) for i in cpu_groups])
    else:
        out_str = ",".join([str(i) for i in cpu_groups])
    return out_str


def main():
    # 创建ArgumentParser对象
    parser = argparse.ArgumentParser(description="device_id")

    # 添加参数
    parser.add_argument("device_id", type=int, help="device_id")
    parser.add_argument("--odd_numa", action="store_true", help="bind service to odd numa node")
    parser.add_argument("--device_type", type=str, default="800i_a2", help="device type: 800i_a2 or 800i_a3")
    args = None

    # 解析命令行参数
    args = parser.parse_args(args)
    odd_numa = args.odd_numa
    device_type = args.device_type
    devices = sorted(list(_get_device_map_info().keys())) # 通过npu-smi info -m 指令获取到所有的chip logic id
    if device_type == "800i_a2":
        out_str = get_cores_a2(args.device_id, devices, odd_numa=odd_numa)
    elif device_type == "800i_a3":
        out_str = get_cores_a3(args.device_id, devices)
    else:
        raise ValueError("Unsupported device type, only 800i_a2, 800i_a3 supported.")
    print(out_str)


if __name__ == "__main__":
    main()