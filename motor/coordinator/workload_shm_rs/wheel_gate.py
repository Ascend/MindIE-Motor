# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Native members inside a packaged motor wheel.

Coordinator has no Python ledger fallback: a deployable wheel must contain the
workload-shm cdylib. kv-conductor is packed when ``build.sh`` produced the binary
(source-dev can still load ``target/release`` without packaging).
"""

import zipfile

# Must match native.py _LIB_BASENAME and setup.py package_data.
WORKLOAD_SHM_WHEEL_MEMBER = "motor/coordinator/workload_shm_rs/lib/libmindie_workload_shm.so"
KV_CONDUCTOR_WHEEL_MEMBER = "motor/kv_conductor/bin/kv-conductor"

_REQUIRED_NATIVE_MEMBERS = (WORKLOAD_SHM_WHEEL_MEMBER,)


def _archive_names(wheel_path: str) -> set[str]:
    with zipfile.ZipFile(wheel_path) as archive:
        return set(archive.namelist())


def list_missing_required_native_libs(wheel_path: str) -> list[str]:
    """Return required native archive members that ``wheel_path`` does not contain."""
    names = _archive_names(wheel_path)
    return [member for member in _REQUIRED_NATIVE_MEMBERS if member not in names]


def assert_motor_wheel_has_workload_shm(wheel_path: str) -> None:
    """Raise ValueError when the wheel is missing the workload-shm cdylib."""
    missing = list_missing_required_native_libs(wheel_path)
    if missing:
        raise ValueError(
            "refusing to emit motor wheel without required native library: "
            + ", ".join(missing)
            + ". Coordinator cannot start without libmindie_workload_shm.so "
            "(no Python ledger fallback). Build it via bash build.sh "
            "(cargo or WORKLOAD_SHM_PREBUILT) before pip wheel."
        )


def assert_motor_wheel_has_kv_conductor(wheel_path: str) -> None:
    """Raise ValueError when kv-conductor was built but not packed into the wheel."""
    if KV_CONDUCTOR_WHEEL_MEMBER not in _archive_names(wheel_path):
        raise ValueError(
            "refusing to emit motor wheel: kv-conductor binary was built but is "
            f"missing from the archive ({KV_CONDUCTOR_WHEEL_MEMBER})."
        )
