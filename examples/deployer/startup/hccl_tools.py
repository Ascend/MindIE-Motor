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
import re
import sys
import json
import time
import shutil
import logging
import subprocess
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any
from enum import Enum

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SERVER_LIST = 'server_list'
MAX_RETRIES = 10
RETRY_INTERVAL = 3


class HardwareType(Enum):
    A2 = 'd802'
    A3 = 'd803'
    UNKNOWN = 'unknown'


def parse_args():
    parser = ArgumentParser(description="Generate hccl config file")
    parser.add_argument(
        "--hccl_path", type=str, default="hccl.json", help="Manually specify the path of hccl config file"
    )
    args = parser.parse_args()
    return args


def get_hardware_type():
    try:
        lspci_path = shutil.which("lspci")
        if not lspci_path:
            raise ValueError("lspci not found!")
        output = subprocess.check_output(f"{lspci_path}", text=True, timeout=5)
        if HardwareType.A2.value in output:
            return HardwareType.A2
        elif HardwareType.A3.value in output:
            return HardwareType.A3
    except EOFError as e:
        logging.error("get hardware type failed: %s", e)

    return HardwareType.UNKNOWN


def get_visible_devices():
    try:
        import glob

        davinci_devices = glob.glob("/dev/davinci*")
        if davinci_devices:
            device_ids = []
            for device_path in davinci_devices:
                match = re.search(r'davinci(\d+)', device_path)
                if match:
                    device_ids.append(match.group(1))
            if device_ids:
                return sorted(device_ids)

    except Exception as e:
        logging.error("Failed to detect visible devices: %s", e)
    return []


def _run_command(cmd_args):
    result = subprocess.run(
        cmd_args,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.stdout:
        return result.stdout.splitlines(keepends=True)
    return []


def retry_command(cmd_args):
    for attempt in range(MAX_RETRIES):
        try:
            result = _run_command(cmd_args)
            if result:
                return result
            logging.warning(
                "Command returned empty result, attempt %d/%d",
                attempt + 1,
                MAX_RETRIES,
            )
        except Exception as e:
            logging.warning(
                "Command failed: %s, attempt %d/%d",
                e,
                attempt + 1,
                MAX_RETRIES,
            )

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_INTERVAL)

    raise ValueError(f"Command failed after {MAX_RETRIES} attempts: {cmd_args}")


def _query_device_ip(device_id):
    """Query single device IP via hccn_tool (runs in parallel per device)."""
    ret_ip = retry_command(["hccn_tool", "-i", str(device_id), "-ip", "-g"])
    logging.info("device_id: %s, device_ip_info: %s", device_id, str(ret_ip))
    device_ip = ret_ip[0].split(":")[1].replace('\n', '').replace(' ', '')
    return device_id, device_ip


def _query_device_sdid(device_id):
    """Query single device super device ID via npu-smi (runs in parallel per device)."""
    card_id = int(device_id) // 2
    chip_id = int(device_id) % 2
    ret_sdid = retry_command(["npu-smi", "info", "-t", "spod-info", "-i", str(card_id), "-c", str(chip_id)])
    logging.info("device_id: %s, super_device_id: %s", device_id, str(ret_sdid))
    device_sdid = ret_sdid[0].split(":")[1].replace('\n', '').replace(' ', '')
    return device_id, device_sdid


def main():
    logging.info("start %s", __file__)
    args = parse_args()

    visible_devices = get_visible_devices()
    logging.info('Detected visible_devices: %s', visible_devices)

    hardware_type = get_hardware_type()
    if hardware_type == HardwareType.UNKNOWN:
        raise ValueError("unknown hardware type!")
    logging.info('Detected hardware_type: %s', hardware_type)

    host_ip = os.getenv('HOST_IP', '127.0.0.1')
    pod_ip = os.getenv('POD_IP', '127.0.0.1')
    logging.info('host_ip: %s', host_ip)
    logging.info('pod_ip: %s', pod_ip)

    device_ips: Dict[Any, Any] = {}
    device_sdids: Dict[Any, Any] = {}
    max_workers = min(len(visible_devices), 16)

    # Phase 1: query device IPs in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        ip_futures = {executor.submit(_query_device_ip, d): d for d in visible_devices}
        for future in as_completed(ip_futures):
            device_id, device_ip = future.result()
            device_ips[device_id] = device_ip

    # Phase 2: query super device IDs in parallel (A3 only)
    if hardware_type == HardwareType.A3:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            sdid_futures = {executor.submit(_query_device_sdid, d): d for d in visible_devices}
            for future in as_completed(sdid_futures):
                device_id, device_sdid = future.result()
                device_sdids[device_id] = device_sdid

    hccn_table = {'version': '1.0', 'server_count': '1', SERVER_LIST: []}
    device_list = []
    for rank_id, device_id in enumerate(visible_devices):
        device_ip = device_ips[device_id]
        device_info = {'device_id': device_id, 'device_ip': device_ip, 'rank_id': str(rank_id)}
        if hardware_type == HardwareType.A3:
            device_info['super_device_id'] = device_sdids[device_id]
        device_list.append(device_info)
        logging.info('rank_id: %s, device_id: %s, device_ip: %s', rank_id, device_id, device_ip)

    hccn_table[SERVER_LIST].append({'server_id': host_ip, 'container_ip': pod_ip, 'device': device_list})

    if hardware_type == HardwareType.A3:
        hccn_table['super_pod_list'] = [{"super_pod_id": "0", SERVER_LIST: [{"server_id": host_ip}]}]

    hccn_table['status'] = 'completed'

    with open(args.hccl_path, 'w', encoding='utf-8') as table_fp:
        json.dump(hccn_table, table_fp, indent=4)
    sys.stdout.flush()
    logging.info("Completed: hccl file was save in : %s", args.hccl_path)


if __name__ == "__main__":
    main()
