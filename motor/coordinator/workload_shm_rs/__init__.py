# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Rust ``mindie-workload-shm`` crate directory, exposed as a Python package only so the compiled
``lib/libmindie_workload_shm.so`` can ship in the wheel (see setup.py ``package_data``). The Rust
source and ``cargo`` build live here; the Python ctypes binding is in
``motor.coordinator.scheduler.runtime.workload_shm.native``.
"""
