# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
from dataclasses import asdict
from pathlib import Path

from motor.config.controller import ControllerConfig
from motor.config.coordinator import CoordinatorConfig
from motor.config.node_manager import NodeManagerConfig
from motor.config.tls_config import TLSConfig


def write_sample_json(dest_file: str) -> None:
    """Create the unified sample JSON."""
    ctrl_data = ControllerConfig().to_dict()
    coord_data = CoordinatorConfig().to_dict()
    nm_data = NodeManagerConfig().to_dict()

    tls_keys = (
        "mgmt_tls_config",
        "infer_tls_config",
        "etcd_tls_config",
        "grpc_tls_config",
        "observability_tls_config",
    )
    gathered_tls = {}
    for cfg in (ctrl_data, coord_data, nm_data):
        for k in tls_keys:
            if k in cfg:
                gathered_tls[k] = cfg.pop(k)

    deploy_src = coord_data.pop("deploy_config", {})
    deploy_part = dict(
        p_instances_num=deploy_src.get("p_instances_num", 1),
        d_instances_num=deploy_src.get("d_instances_num", 1),
        single_p_instance_pod_num=1,
        single_d_instance_pod_num=1,
        p_pod_npu_num=16,
        d_pod_npu_num=16,
        image_name="",
        job_id="mindie-motor",
        hardware_type="800I_A3",
        weight_mount_path="/mnt/weight/",
    )

    empty_tls = {k: ("" if isinstance(v, str) else False) for k, v in asdict(TLSConfig()).items()}
    deploy_part["tls_config"] = {k: gathered_tls.get(k, empty_tls.copy()) for k in tls_keys}

    out_obj = {
        "version": "v2.0",
        "motor_deploy_config": deploy_part,
        "motor_controller_config": ctrl_data,
        "motor_coordinator_config": coord_data,
        "motor_nodemanger_config": nm_data,
    }

    path_obj = Path(dest_file)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text(json.dumps(out_obj, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    out = Path(__file__).parent.parent.parent / "examples" / "features" / "config_sample.json"
    write_sample_json(str(out))
