# End-to-End Testing — Patterns & Conventions

## Current E2E Coverage

`tests/e2e/` holds pytest-based end-to-end tests that exercise **cross-component flows** with mocked process boundaries:

| Test file | What it covers |
|-----------|----------------|
| `tests/e2e/test_prestop_e2e.py` | PreStop graceful-shutdown chain: `HeartbeatManager.pause_all_endpoints/resume_all_endpoints` → Controller `InstanceManager` PAUSED state machine → Controller `EventPusher` PAUSE/RESUME routing → Coordinator `InstanceManager` `_paused_pool` operations (37 tests) |

Run them like any other module:

```bash
bash tests/run_tests.sh tests/e2e/
```

These are in-process pytest tests (no real daemon processes or K8s). True cluster-level deployment testing lives in the `motor-e2e` skill (`examples/deployer/`), not in `tests/e2e/`.

## Port Allocation for E2E Tests

When an E2E test needs real HTTP ports, use a high range (20025-20028) to avoid conflicts with production services:

| Component | Port | Config Key |
|-----------|------|------------|
| Coordinator Infer | 20025 | `coordinator_api_infer_port` |
| Coordinator Mgmt | 20026 | `coordinator_api_mgmt_port` |
| Coordinator Obs  | 20027 | `coordinator_obs_port` |
| Controller API | 20028 | `controller_api_port` |

## Adding New E2E Tests

Follow these conventions when adding a new E2E test:

1. **Test file**: place in `tests/e2e/test_<flow>_e2e.py` with a descriptive name (e.g. `test_prestop_e2e.py`)
2. **Cross-component scope**: an E2E test must span at least two components (e.g. Controller state machine → Coordinator pool) — single-component tests belong in `tests/<module>/`
3. **Mock process boundaries**: components communicate via mocked clients/HTTP; do not spawn real daemon processes in unit-level e2e tests
4. **Pre-mock generated protobufs**: if the test imports `motor.common.etcd`, pre-mock the `*_pb2`/`*_pb2_grpc` modules with `MagicMock` before import (see `test_prestop_e2e.py`) — they are not compiled in dev environments
5. **Assertions**: assert on state transitions and event payloads, not just return codes

### Template

```python
# tests/e2e/test_<flow>_e2e.py
import sys
from unittest.mock import MagicMock, patch

import pytest

# Pre-mock protobuf generated modules (not compiled in dev environment)
_mock_pb2 = MagicMock()
_mock_pb2_grpc = MagicMock()
sys.modules["motor.common.etcd.proto.rpc_pb2"] = _mock_pb2
sys.modules["motor.common.etcd.proto.rpc_pb2_grpc"] = _mock_pb2_grpc

from motor.common.resources.instance import Instance, PDRole  # noqa: E402


class TestFlowE2E:
    """End-to-end test for <flow>."""

    def test_flow_end_to_end(self):
        """Component A state change propagates to component B."""
        ...
```
