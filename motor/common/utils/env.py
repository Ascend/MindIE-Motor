#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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


Env = _Environment()
