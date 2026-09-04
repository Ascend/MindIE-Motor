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
from pathlib import Path

from setuptools import find_packages, setup


def _read_version() -> str:
    init_py = Path(__file__).resolve().parent / "motor" / "__init__.py"
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        init_py.read_text(encoding="utf-8"),
        re.M,
    )
    if not match:
        raise RuntimeError(f"Unable to find __version__ in {init_py}")
    return match.group(1)


# Conditionally include the kv-conductor binary when it was built (see build.sh).
# When the Rust toolchain is unavailable, the binary does not exist and the
# wheel is packaged without it — the Python runtime handles this gracefully.
_package_data: dict[str, list[str]] = {
    "motor": ["version.info"],
}

_kv_bin = os.path.join("motor", "kv_conductor", "bin", "kv-conductor")
if os.path.isfile(_kv_bin):
    _package_data["motor.kv_conductor"] = ["bin/kv-conductor"]

# Conditionally include the workload-shm cdylib when it was built (see build.sh). Missing at import
# time surfaces as an explicit runtime error (native.py), never a silent wrong-ledger fallback.
_shm_so = os.path.join("motor", "coordinator", "workload_shm_rs", "lib", "libmindie_workload_shm.so")
if os.path.isfile(_shm_so):
    _package_data["motor.coordinator.workload_shm_rs"] = ["lib/*"]

setup(
    name="motor",
    version=_read_version(),
    description="A Python package named motor.",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[],
    package_data=_package_data,
    include_package_data=True,
    zip_safe=False,
)
