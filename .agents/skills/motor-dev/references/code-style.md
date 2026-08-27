# Code Style — Complete Guide

## License Header (required on every Python file)

Every Python file must start with the Mulan PSL v2 license header. No comments or descriptions before it.

**Markdown documents (`.md`) do NOT need a license header** — they start directly with the document title (`# ...`). Keep doc style consistent with the existing `docs/` tree (no header, no trailing license comment).

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
```

## Logging

**Never use f-strings in logger calls.** The logging framework defers string formatting until the message is actually emitted. Using `%s`/`%d` placeholders avoids wasted CPU on debug-level messages that never get logged.

```python
# Correct — formatting deferred until log level check passes
logger.info("Instance %s started on port %d", name, port)
logger.warning("Retry %d of %d for %s", attempt, max_retries, instance_id)
logger.error("Failed to connect to %s:%d — %s", host, port, error_msg)
logger.debug("Processing request %s with params %s", request_id, params)

# Wrong — f-string formatting always happens, even for disabled log levels
logger.info(f"Instance {name} started on port {port}")
```

## Class Member Order

Follow this strict ordering within classes:

1. `__init__` — constructor
2. `@staticmethod` methods — no self/cls access
3. `@classmethod` methods — cls access only
4. Public methods — external API (e.g. `start`, `stop`, `get_status`)
5. Private methods — internal implementation (`_prefix`, e.g. `_handle_loop`)
6. `@property` — computed attributes

```python
from typing import Any

class ExampleService:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._state: dict[str, Any] | None = None

    @staticmethod
    def validate_port(port: int) -> bool:
        return 1024 <= port <= 65535

    def start(self) -> None:
        self._initialize()

    def _initialize(self) -> None:
        self._state = {}

    @property
    def is_running(self) -> bool:
        return self._state is not None
```

## Python Style Rules

| Rule | Value |
|------|-------|
| Line length | max 120 chars (ruff) |
| Indentation | 4 spaces, no tabs |
| Imports order | stdlib → third-party → local (blank line between groups) |
| Naming: classes | PascalCase (`InstanceManager`) |
| Naming: functions | snake_case (`get_instance`) |
| Naming: constants | UPPER_SNAKE_CASE (`MAX_RETRIES`) |
| Naming: private | `_prefix` (`_internal_state`) |
| Type hints | required on all parameters and return values |
| Docstrings | triple double-quotes, Google/NumPy style |

### Type Hint Patterns

**Use Python 3.10+ native syntax only.** The pre-commit hook `check-modern-typing` (see `pre-commit/check_modern_typing.py`) auto-detects and rewrites old-style typing to native syntax, so `Optional[X]`/`Union[X, Y]`/`Dict[K, V]`/`List[X]` will fail CI review. The only exception is `deployer/` (excluded by policy).

```python
# Correct — Python 3.10+ native syntax, Any imported from typing when needed
from typing import Any

def get_instance(instance_id: str, timeout: int = 30) -> dict[str, Any] | None:
    """Fetch instance metadata.

    Args:
        instance_id: The unique instance identifier.
        timeout: Maximum wait time in seconds.

    Returns:
        Instance dict if found, None otherwise.

    Raises:
        ValueError: If instance_id is empty.
    """
    ...
```

```python
# Wrong — old-style typing; rejected by pre-commit check-modern-typing
from typing import Optional, Union, Dict, List, Tuple, Set, FrozenSet, Type

def get_instance(instance_id: str, timeout: int = 30) -> Optional[dict[str, Any]]:
    ...
```

### Modern Python Typing (pre-commit enforced)

`pre-commit/check_modern_typing.py` rewrites these forms automatically:

| Old style (typing module) | Python 3.10+ native |
|---------------------------|----------------------|
| `Optional[X]` | `X \| None` |
| `Union[X, Y]` | `X \| Y` |
| `Dict[K, V]` | `dict[K, V]` |
| `List[X]` | `list[X]` |
| `Tuple[X, ...]` | `tuple[X, ...]` |
| `Set[X]` | `set[X]` |
| `FrozenSet[X]` | `frozenset[X]` |
| `Type[X]` | `type[X]` |

The hook also removes now-unused `from typing import ...` lines. `Any` remains the only commonly-needed `typing` import (use `from typing import Any`). Never write `any` (the builtin function) in a type position — it is not a type and mypy/pyright reject it.

### ThreadSafeSingleton Pattern

Used by all core components (InstanceManager, FaultManager, RegisterManager, HeartbeatManager, MetricsCollector):

```python
class MyComponent(ThreadSafeSingleton):
    def __init__(self, config: CoordinatorConfig | None = None) -> None:
        super().__init__()
        if hasattr(self, "_initialized"):
            return  # Already initialized (singleton reuse)
        # ... initialization ...
        self._initialized = True
```

## Markdown Format Rules

| Rule | Description |
|------|-------------|
| MD022 | Headings surrounded by blank lines |
| MD031 | Fenced code blocks surrounded by blank lines |
| MD032 | Lists surrounded by blank lines |
| MD034 | No bare URLs — use `[text](url)` |
| MD040 | Code blocks must have language identifiers |
| — | Line length: max 120 chars |
