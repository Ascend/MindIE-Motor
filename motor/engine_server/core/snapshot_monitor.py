# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading

from motor.common.utils.singleton import ThreadSafeSingleton


class SnapshotMonitor(ThreadSafeSingleton):
    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._flags_lock = threading.Lock()
        self._suspend_done = False
        self._unlock_done = False
        self._resume_done = False
        self._initialized = True

    @property
    def is_suspend_done(self) -> bool:
        with self._flags_lock:
            return self._suspend_done

    @property
    def is_unlock_done(self) -> bool:
        with self._flags_lock:
            return self._unlock_done

    @property
    def is_resume_done(self) -> bool:
        with self._flags_lock:
            return self._resume_done

    def mark_suspend_done(self) -> None:
        with self._flags_lock:
            self._suspend_done = True

    def mark_unlock_done(self) -> None:
        with self._flags_lock:
            self._unlock_done = True

    def mark_resume_done(self) -> None:
        with self._flags_lock:
            self._resume_done = True
