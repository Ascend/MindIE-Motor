# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Protocol contracts between the Daemon and its managed services."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class DaemonService(Protocol):
    """Minimal interface every daemon-managed service must satisfy.

    ``stop()`` must be safe to call multiple times (idempotent).
    ``health_check()`` is called periodically by the Daemon process monitor;
    each service encapsulates its own failure detection and self-restart logic.
    """

    def stop(self) -> None:
        """Stop the service.  Must be idempotent."""
        ...

    def health_check(self) -> list | None:
        """Check service health and self-restart if needed.

        Called by the Daemon process monitor on every tick (~5 s).
        The implementation handles its own failure detection and recovery
        (e.g. ``os.kill`` for subprocess PIDs, ``thread.is_alive`` for threads).

        Returns a list of death events ``[(pid, endpoint_id), ...]`` for
        services with subprocesses (the engine service); other services may
        return None.
        """
        ...


@runtime_checkable
class PreparableService(DaemonService, Protocol):
    """A service that needs pre-flight preparation before the engine starts."""

    def prepare(self, **kwargs) -> None:
        """Run before ``NativeEngineService.pull()``.

        *kwargs* include ``endpoints_count`` (int) so the service can
        divide per-node DRAM across DP ranks.
        """
        ...
