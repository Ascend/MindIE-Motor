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


class _Environment:
    @property
    def job_name(self):
        return os.getenv("JOB_NAME", None)

    @property
    def config_path(self):
        return os.getenv("CONFIG_PATH", None)

    @property
    def hccl_path(self):
        return os.getenv("HCCL_PATH", None)

    @property
    def ranktable_path(self):
        return os.getenv("RANKTABLE_PATH", None)

    @property
    def user_config_path(self):
        return os.getenv("USER_CONFIG_PATH", None)

    @property
    def role(self):
        return os.getenv("ROLE", None)

    @property
    def index(self):
        return os.getenv("INDEX", None)

    @property
    def pod_ip(self):
        return os.getenv("POD_IP", None)

    @property
    def coordinator_service(self):
        return os.getenv(
            "COORDINATOR_SERVICE",
            "mindie-motor-coordinator-service.mindie-motor.svc.cluster.local",
        )

    @property
    def coordinator_infer_service(self):
        return os.getenv(
            "COORDINATOR_INFER_SERVICE",
            os.getenv(
                "COORDINATOR_SERVICE",
                "mindie-motor-coordinator-service.mindie-motor.svc.cluster.local",
            ),
        )

    @property
    def coordinator_obs_service(self):
        return os.getenv(
            "COORDINATOR_OBS_SERVICE",
            os.getenv(
                "COORDINATOR_SERVICE",
                "mindie-motor-coordinator-service.mindie-motor.svc.cluster.local",
            ),
        )

    @property
    def controller_service(self):
        return os.getenv(
            "CONTROLLER_SERVICE",
            "mindie-motor-controller-service.mindie-motor.svc.cluster.local",
        )

    @property
    def conductor_service(self):
        return os.getenv("KV_CONDUCTOR_SERVICE", "")

    # --- KV store ---

    @property
    def kv_store_backend(self):
        return os.getenv("KV_STORE_BACKEND", "")

    @property
    def kv_cache_store_port(self):
        return os.getenv("KV_CACHE_STORE_PORT", "")

    @property
    def kvs_master_service(self):
        return os.getenv("KVS_MASTER_SERVICE", "")

    # --- Daemon behaviour ---

    @property
    def motor_restart_engine(self):
        return os.getenv("MOTOR_RESTART_ENGINE", "0") == "1"

    @property
    def motor_restart_local_service(self):
        return os.getenv("MOTOR_RESTART_LOCAL_SERVICE", "1") == "1"

    # --- Memcache LocalService ---

    @property
    def mmc_local_config_path(self):
        return os.getenv("MMC_LOCAL_CONFIG_PATH", "")

    @property
    def mmc_local_service_mode(self):
        return os.getenv("MMC_LOCAL_SERVICE_MODE", "")

    @property
    def mmc_dram_size(self):
        return os.getenv("MMC_DRAM_SIZE", "")

    @property
    def mmc_config_store_url(self):
        return os.getenv("MMC_CONFIG_STORE_URL", "")


Env = _Environment()
