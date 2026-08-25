# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for the store-process bootstraps (bootstrap/ascend*).

The real ``acl`` is not available in CI; the modules are exercised with a
stand-in ``acl`` module injected into ``sys.modules``. What matters here: the
ACL context is created before the store service runs, and every failure is
non-fatal — the store must always start.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from motor.node_manager.core.services.mooncake.bootstrap import ascend_800I, ascend_850


class _FakeACL:
    """Minimal stand-in for the real ``acl`` module (not installed in CI)."""

    def __init__(self, device_count=2, device_count_ret=0):
        self.device_count = device_count
        self.device_count_ret = device_count_ret
        self.calls = []
        self.rt = MagicMock()
        self.rt.get_device_count.side_effect = lambda: (self.device_count, self.device_count_ret)

    def init(self):
        self.calls.append("init")


@pytest.fixture
def fake_acl():
    fake = _FakeACL()
    with patch.dict(sys.modules, {"acl": fake}):
        yield fake


def _run_module_mock(module):
    """Context manager patching runpy.run_module inside a bootstrap module."""
    return patch.object(module.runpy, "run_module")


# ===================================================================
# ascend_800I — ACL context only, no HCCL comm
# ===================================================================


def test_800I_bootstrap_sets_up_acl_and_runs_store(fake_acl):
    """ACL context first, then the official store service."""
    with _run_module_mock(ascend_800I) as run_module:
        ascend_800I.main()
    run_module.assert_called_once_with("mooncake.mooncake_store_service", run_name="__main__")
    assert fake_acl.calls == ["init"]
    fake_acl.rt.set_device.assert_called_once_with(0)


def test_800I_bootstrap_continues_when_acl_unavailable(caplog):
    """acl import/init failure is non-fatal: the store still starts."""
    with patch.dict(sys.modules, {"acl": None}), _run_module_mock(ascend_800I) as run_module:
        ascend_800I.main()
    run_module.assert_called_once()
    assert any("ACL context init failed (non-fatal)" in record.message for record in caplog.records)


# ===================================================================
# ascend_850 — ACL context only, comm-free like 800I (pending A5 re-validation)
# ===================================================================


def test_850_bootstrap_sets_up_acl_and_runs_store(fake_acl):
    """ACL context first, then the official store service."""
    with _run_module_mock(ascend_850) as run_module:
        ascend_850.main()
    run_module.assert_called_once_with("mooncake.mooncake_store_service", run_name="__main__")
    assert fake_acl.calls == ["init"]
    fake_acl.rt.set_device.assert_called_once_with(0)


def test_850_bootstrap_continues_when_acl_unavailable(caplog):
    """acl import/init failure is non-fatal: the store still starts."""
    with patch.dict(sys.modules, {"acl": None}), _run_module_mock(ascend_850) as run_module:
        ascend_850.main()
    run_module.assert_called_once()
    assert any("ACL context init failed (non-fatal)" in record.message for record in caplog.records)
