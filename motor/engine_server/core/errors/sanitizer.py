# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Lightweight error-message sanitization for engine error adapters.

This module must stay free of TLS/HTTP client imports so error handling cannot
fail again while trying to sanitize a message.
"""

from __future__ import annotations

import re

_FILE_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s]+|/[^\s]+")
_FILE_LOCATION_RE = re.compile(r'File "[^"]+", line \d+')
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):.*", flags=re.DOTALL)


def sanitize_error_message(error_msg: str) -> str:
    """Strip paths, traceback fragments, and truncate overlong messages."""
    try:
        from motor.common.http.security_utils import sanitize_error_message as project_sanitize

        return project_sanitize(error_msg)
    except ImportError:
        pass

    message = _FILE_PATH_RE.sub("[FILE_PATH]", error_msg)
    message = _FILE_LOCATION_RE.sub("[FILE_LOCATION]", message)
    message = _TRACEBACK_RE.sub("", message)
    message = message.strip()
    if not message:
        return "An internal error occurred"
    if len(message) > 200:
        return message[:200] + "..."
    return message
