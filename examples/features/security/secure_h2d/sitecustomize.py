# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys


_TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _secure_patch_enabled() -> bool:
    return os.getenv("SECURE_PATCH_ENABLE", "0").strip().lower() in _TRUE_VALUES


def _is_cpuinfo_json_process() -> bool:
    if not sys.argv:
        return False

    argv = [str(item) for item in sys.argv]
    argv0 = argv[0].replace("\\", "/").lower()

    return "--json" in argv and (
        "cpuinfo/cpuinfo.py" in argv0 or argv0.endswith("/cpuinfo.py") or argv0.endswith("cpuinfo.py")
    )


if _secure_patch_enabled() and not _is_cpuinfo_json_process():
    try:
        from secure_patch.installer import install_all

        install_all()
    except Exception as exc:
        print(
            f"[sitecustomize] secure_patch install failed: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        raise
