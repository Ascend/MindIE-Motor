# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
import socket

MOTOR_SNAPSHOT_WORKSPACE_DIR = "/snapshot"
MOTOR_SNAPSHOT_METADATA_PATH = os.path.join(MOTOR_SNAPSHOT_WORKSPACE_DIR, "snapshot_metadata.json")
MOTOR_SNAPSHOT_WEIGHT_DIR = os.path.join(MOTOR_SNAPSHOT_WORKSPACE_DIR, "weight")
MOTOR_SNAPSHOT_CONFIGMAP_DIR = os.path.join(MOTOR_SNAPSHOT_WORKSPACE_DIR, "configmap")

RETRY_LOG_FREQUENCY = 60

RESTORED_FLAG_PATH = "/root/.grusflag"


def is_restored_from_host_side_snapshot() -> bool:
    return os.path.exists(RESTORED_FLAG_PATH)


def load_snapshot_metadata(file_path: str, field_name: str) -> str:
    if not os.path.exists(file_path):
        raise FileNotFoundError("Snapshot metadata file not found: %s" % file_path)

    with open(file_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception as e:
            raise ValueError("Snapshot metadata is not valid JSON: %s: %s" % (file_path, e)) from e

        if not isinstance(data, dict):
            raise ValueError("Snapshot metadata JSON root must be an object, not an array or scalar: %s" % file_path)

        field_value = data.get(field_name, None)
        if not isinstance(field_value, str):
            raise ValueError(
                "Snapshot metadata requires string field: %s, but got %s" % (field_name, type(field_value))
            )

        return field_value


def update_snapshot_metadata(file_path: str, field_name: str, field_value: str) -> None:
    if not os.path.exists(file_path):
        raise FileNotFoundError("Snapshot metadata file not found: %s" % file_path)

    with open(file_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception as e:
            raise ValueError("Snapshot metadata is not valid JSON: %s: %s" % (file_path, e)) from e

        if not isinstance(data, dict):
            raise ValueError("Snapshot metadata JSON root must be an object, not an array or scalar: %s" % file_path)

        data[field_name] = field_value

    with open(file_path, "w", encoding="utf-8") as f:
        try:
            json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            raise ValueError(
                "Failed to write field %s (value: %s) to snapshot metadata file %s: %s"
                % (field_name, field_value, file_path, e)
            ) from e


def get_pod_ip() -> str:
    targets = [("8.8.8.8", 80), ("2001:4860:4860::8888", 80)]

    for t in targets:
        s = socket.socket(socket.AF_INET6 if ":" in t[0] else socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(t)
            return s.getsockname()[0]
        except OSError:
            pass
        finally:
            s.close()

    raise RuntimeError("Failed to detect pod IP via external connectivity probes")
