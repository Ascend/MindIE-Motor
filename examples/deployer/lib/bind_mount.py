# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Parse docker ``-v`` bind specs without importing the docker or Render modules."""

from __future__ import annotations

import os
import re

_BIND_OPTIONS = {
    "ro",
    "rw",
    "z",
    "Z",
    "shared",
    "slave",
    "private",
    "rshared",
    "rslave",
    "rprivate",
    "consistent",
    "delegated",
    "cached",
    "nocopy",
}


def _expand_bind_token(raw: str, env: dict[str, str] | None) -> str:
    names = env or {}

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return (names.get(name) or os.environ.get(name) or "").strip()

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, raw).strip()


def bind_mount_destination(spec: str, env: dict[str, str] | None = None) -> str | None:
    """Container path of a ``-v host:container[:opt]`` spec, or None if it cannot be read."""
    text = spec.strip().strip('"').strip("'")
    if not text or ":" not in text:
        return None
    parts = text.split(":")
    if len(parts) >= 3 and "/" not in parts[-1]:
        tokens = [token for token in parts[-1].split(",") if token]
        if tokens and all(token in _BIND_OPTIONS for token in tokens):
            parts = parts[:-1]
    if len(parts) < 2:
        return None
    dest = _expand_bind_token(":".join(parts[1:]), env)
    if not dest:
        return None
    return os.path.normpath(dest)


def bind_mount_is_new(spec: str, seen: set[str], env: dict[str, str] | None = None) -> bool:
    """True when this bind's container path is not already in ``seen``. Unknown specs are kept."""
    dest = bind_mount_destination(spec, env)
    if dest is None:
        return True
    if dest in seen:
        return False
    seen.add(dest)
    return True
