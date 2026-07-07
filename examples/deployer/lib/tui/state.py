# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Thread-safe per-pod progress state store."""

from __future__ import annotations

import threading
import time


class PodProgressState:
    """Shared, thread-safe store for per-pod progress."""

    _MAX_ERRORS = 50  # keep at most this many error lines per pod

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    @staticmethod
    def _make_entry() -> dict:
        return {
            "progress": 0,
            "last_step": "",
            "line_index": 0,
            "error_count": 0,
            "error_lines": [],  # list of (line_index, message)
            "started_at": time.monotonic(),
        }

    def register(self, pod_name: str) -> None:
        with self._lock:
            self._data.setdefault(pod_name, self._make_entry())

    def update(self, pod_name: str, progress: int, last_step: str = "") -> None:
        with self._lock:
            entry = self._data.setdefault(pod_name, self._make_entry())
            if progress > 0:
                entry["progress"] = progress
            if last_step:
                entry["last_step"] = last_step

    def increment_line(self, pod_name: str) -> None:
        with self._lock:
            entry = self._data.get(pod_name)
            if entry is not None:
                entry["line_index"] += 1

    def remove(self, pod_name: str) -> None:
        """Remove a pod entry (e.g. when the pod was replaced after restart)."""
        with self._lock:
            self._data.pop(pod_name, None)

    def add_error(self, pod_name: str, error_line: str, line_index: int) -> None:
        """Record an error line with its line-index for display."""
        with self._lock:
            entry = self._data.get(pod_name)
            if entry is None:
                return
            entry["error_count"] += 1
            entry["error_lines"].append((line_index, error_line))
            if len(entry["error_lines"]) > self._MAX_ERRORS:
                entry["error_lines"] = entry["error_lines"][-self._MAX_ERRORS :]

    def get_all(self) -> dict[str, dict]:
        with self._lock:
            result = {}
            for k, v in self._data.items():
                entry = dict(v)
                entry["error_lines"] = list(v["error_lines"])
                result[k] = entry
            return result

    @property
    def all_completed(self) -> bool:
        with self._lock:
            if not self._data:
                return False
            return all(v["progress"] >= 100 for v in self._data.values())
