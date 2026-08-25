# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import subprocess
import sys


def test_models_and_domain_public_api_import_in_fresh_interpreter():
    """Models may load first without domain package exports causing a circular import."""
    code = """
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.domain import InstanceReadiness, RequestManager, calculate_demand_workload

assert RequestInfo.__name__ == "RequestInfo"
assert InstanceReadiness.__name__ == "InstanceReadiness"
assert RequestManager.__name__ == "RequestManager"
assert calculate_demand_workload.__name__ == "calculate_demand_workload"
"""

    subprocess.run([sys.executable, "-c", code], check=True)
