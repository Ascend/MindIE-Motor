# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from __future__ import annotations

import functools
import threading
from typing import Callable, Dict

from .config import CONFIG

_PATCHED: Dict[str, object] = {}
_GUARD = threading.local()


def log(msg: str) -> None:
    if CONFIG.debug:
        print(f"[secure_patch] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[secure_patch][WARN] {msg}", flush=True)


class guard:
    def __init__(self, name: str):
        self.name = name
        self.already = False

    def __enter__(self):
        active = getattr(_GUARD, "active", None)
        if active is None:
            active = set()
            _GUARD.active = active
        self.already = self.name in active
        active.add(self.name)
        return not self.already

    def __exit__(self, exc_type, exc, tb):
        active = getattr(_GUARD, "active", set())
        active.discard(self.name)
        return False


def remember_patch(name: str, original: object) -> bool:
    if name in _PATCHED:
        return False
    _PATCHED[name] = original
    return True


def safe_patch(name: str, installer: Callable[[], bool]) -> bool:
    if name in _PATCHED:
        return False
    try:
        ok = installer()
        if ok:
            log(f"installed patch: {name}")
        return ok
    except Exception as exc:
        warn(f"install patch {name} failed: {exc!r}")
        if CONFIG.strict:
            raise
        return False


def wrap_errors(patch_name: str, original: Callable):
    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                warn(f"{patch_name} failed: {exc!r}")
                if CONFIG.strict:
                    raise
                return original(*args, **kwargs)

        return wrapper

    return decorator
