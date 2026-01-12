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

from .validate_utils import (
    is_valid_path,
    is_valid_integer,
    is_valid_bool,
    is_valid_str,
    is_valid_mount,
    PathValidationConfig
)

TLS_CERT = "tls_cert"
TLS_KEY = "tls_key"
TLS_CRL = "tls_crl"
TLS_PASSWD = "tls_passwd"
TLS_ENABLE = "tls_enable"
CA_CERT = "ca_cert"
CLUSTER_TLS_ITEMS = "cluster_tls_items"
MANAGEMENT_TLS_ITEMS = "management_tls_items"
ETCD_SERVER_TLS_ITEMS = "etcd_server_tls_items"
INFER_TLS_ITEMS = "infer_tls_items"
TLS_CONFIG = "tls_config"
DEPLOY_MOUNT_PATH = "deploy_mount_path"


def validate_deploy_config(config_dict):
    deploy_config = config_dict["deploy_config"]

    # Get hardware type
    hardware_type = deploy_config["hardware_type"]

    if hardware_type not in ["800I_A2", "800I_A3"]:
        raise ValueError(f"Invalid hardware_type: {hardware_type}. Must be '800I_A2' or '800I_A3'.")

    is_valid_integer("p_instances_num", deploy_config["p_instances_num"], 1, 20)
    is_valid_integer("d_instances_num", deploy_config["d_instances_num"], 1, 20)
    is_valid_integer("single_p_instance_pod_num", deploy_config["single_p_instance_pod_num"], 1, 8)
    is_valid_integer("single_d_instance_pod_num", deploy_config["single_d_instance_pod_num"], 1, 20)
    is_valid_integer("p_pod_npu_num", deploy_config["p_pod_npu_num"], 1, 16)
    is_valid_integer("d_pod_npu_num", deploy_config["d_pod_npu_num"], 1, 16)
    is_valid_integer("p_instances_scale_num", deploy_config["p_instances_scale_num"], -20, 20)
    is_valid_integer("d_instances_scale_num", deploy_config["d_instances_scale_num"], -20, 20)
    is_valid_str("model_id", deploy_config["model_id"], 0, 1024)
    is_valid_integer("prefill_distribute_enable", deploy_config["prefill_distribute_enable"], 0, 1)
    is_valid_integer("decode_distribute_enable", deploy_config["decode_distribute_enable"], 0, 1)
    is_valid_str("image_name", deploy_config["image_name"], 1, 1024)
    is_valid_str("job_id", deploy_config["job_id"], 1, 1024)
    is_valid_path("mindie_env_path", deploy_config["mindie_env_path"])

    config = PathValidationConfig(allow_empty=True)
    is_valid_path("mindie_host_log_path", deploy_config["mindie_host_log_path"], config)

    is_valid_path("mindie_container_log_path", deploy_config["mindie_container_log_path"])
    is_valid_path("weight_mount_path", deploy_config["weight_mount_path"])
    is_valid_bool("function_enable", deploy_config["coordinator_backup_cfg"]["function_enable"])
    is_valid_bool("function_sw", deploy_config["controller_backup_cfg"]["function_sw"])
    is_valid_mount("ms_controller_mount", deploy_config[DEPLOY_MOUNT_PATH]["ms_controller_mount"])
    is_valid_mount("ms_coordinator_mount", deploy_config[DEPLOY_MOUNT_PATH]["ms_coordinator_mount"])
    is_valid_mount("prefill_server_mount", deploy_config[DEPLOY_MOUNT_PATH]["prefill_server_mount"])
    is_valid_mount("decode_server_mount", deploy_config[DEPLOY_MOUNT_PATH]["decode_server_mount"])

    if deploy_config[TLS_CONFIG][TLS_ENABLE] is not None:
        is_valid_bool(TLS_ENABLE, deploy_config[TLS_CONFIG][TLS_ENABLE])
    is_valid_bool("cluster_tls_enable", deploy_config[TLS_CONFIG]["cluster_tls_enable"])
    is_valid_bool("etcd_server_tls_enable", deploy_config[TLS_CONFIG]["etcd_server_tls_enable"])
    is_valid_bool("infer_tls_enable", deploy_config[TLS_CONFIG]["infer_tls_enable"])
    is_valid_bool("management_tls_enable", deploy_config[TLS_CONFIG]["management_tls_enable"])

    is_valid_path("infer_ca_cert", deploy_config[TLS_CONFIG][INFER_TLS_ITEMS][CA_CERT])
    is_valid_path("infer_tls_cert", deploy_config[TLS_CONFIG][INFER_TLS_ITEMS][TLS_CERT])
    is_valid_path("infer_tls_key", deploy_config[TLS_CONFIG][INFER_TLS_ITEMS][TLS_KEY])
    is_valid_path("infer_tls_passwd", deploy_config[TLS_CONFIG][INFER_TLS_ITEMS][TLS_PASSWD])
    is_valid_str("infer_tls_crl", deploy_config[TLS_CONFIG][INFER_TLS_ITEMS][TLS_CRL], 0, 128)

    is_valid_path("manager_ca_cert", deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ITEMS][CA_CERT])
    is_valid_path("manager_tls_cert", deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ITEMS][TLS_CERT])
    is_valid_path("manager_tls_key", deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ITEMS][TLS_KEY])
    is_valid_path("manager_tls_passwd", deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ITEMS][TLS_PASSWD])
    is_valid_str("manager_tls_crl", deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ITEMS][TLS_CRL], 0, 128)

    is_valid_path("cluster_ca_cert", deploy_config[TLS_CONFIG][CLUSTER_TLS_ITEMS][CA_CERT])
    is_valid_path("cluster_tls_cert", deploy_config[TLS_CONFIG][CLUSTER_TLS_ITEMS][TLS_CERT])
    is_valid_path("cluster_tls_key", deploy_config[TLS_CONFIG][CLUSTER_TLS_ITEMS][TLS_KEY])
    is_valid_path("cluster_tls_passwd", deploy_config[TLS_CONFIG][CLUSTER_TLS_ITEMS][TLS_PASSWD])
    is_valid_str("cluster_tls_crl", deploy_config[TLS_CONFIG][CLUSTER_TLS_ITEMS][TLS_CRL], 0, 128)

    is_valid_path("etcd_server_ca_cert", deploy_config[TLS_CONFIG][ETCD_SERVER_TLS_ITEMS][CA_CERT])
    is_valid_path("etcd_server_tls_cert", deploy_config[TLS_CONFIG][ETCD_SERVER_TLS_ITEMS][TLS_CERT])
    is_valid_path("etcd_server_tls_key", deploy_config[TLS_CONFIG][ETCD_SERVER_TLS_ITEMS][TLS_KEY])
    is_valid_path("etcd_server_tls_passwd", deploy_config[TLS_CONFIG][ETCD_SERVER_TLS_ITEMS][TLS_PASSWD])
    is_valid_str("etcd_server_tls_crl", deploy_config[TLS_CONFIG][ETCD_SERVER_TLS_ITEMS][TLS_CRL], 0, 128)


def validate_user_config(config_dict: dict):
    validate_deploy_config(config_dict)