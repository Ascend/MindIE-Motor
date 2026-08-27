# Testing Guide — Patterns & Conventions

## Test Script Options

| Flag | Effect | Example |
|------|--------|---------|
| `-v, --verbose` | Detailed output | `bash tests/run_tests.sh -v tests/controller/` |
| `-s` | Show test stdout/stderr | `bash tests/run_tests.sh -s tests/config/` |
| `-x` | Stop on first failure | `bash tests/run_tests.sh -x tests/config/` |
| `-n NUM` | Parallel workers (default: 6) | `bash tests/run_tests.sh -n 4 tests/` |
| `--serial` | Disable parallel execution | `bash tests/run_tests.sh --serial tests/` |
| `--cov` | Enable coverage | `bash tests/run_tests.sh --cov tests/` |
| `-k EXPRESSION` | Run matching tests only | `bash tests/run_tests.sh -k "test_register"` |

### Common Command Patterns

```bash
# Quick focused run
bash tests/run_tests.sh -x tests/controller/core/test_instance_manager.py

# Coverage for a module
bash tests/run_tests.sh -v --cov tests/coordinator/

# Find and run related tests
bash tests/run_tests.sh -k "heartbeat" tests/

# Serial for debugging (parallel can hide tracebacks)
bash tests/run_tests.sh --serial -s tests/node_manager/
```

## Test Coverage Requirements

When adding new logic, create tests covering at minimum:

| Scenario | Example Input | Expected Behavior |
|----------|--------------|-------------------|
| Normal case | Default parameters, typical input | Success, correct output |
| Parameter variations | Each new parameter value | Correct behavior per variant |
| Cache hit / miss | Within TTL, expired TTL | Hit returns cached, miss fetches fresh |
| API failure | Network error, HTTP 500 | Graceful degradation, clear error |
| Edge cases | Empty input, missing keys | No crash, sensible default |
| Error handling | Exceptions from dependencies | Caught, logged, propagated |

## Test Design Principles

Four rules, applied **before** writing any test (also summarized in SKILL.md):

1. **Design before you write** — answer four questions first: what is the module for, what is its I/O contract, what failure am I guarding against, and what is the cheapest level that catches it (unit > integration > e2e)?
2. **Reuse before create** — extend the existing test file and `conftest.py` fixtures; add a new file only when no nearby suite fits.
3. **Test behavior with intent** — assert observable outcomes through public APIs; state why in the name/docstring. Skip trivial wiring; flaky tests are worse than no tests.
4. **Keep it minimal** — one behavior per test, smallest setup that triggers it; if the test diff dwarfs the code change, cut scope.

```python
# Good — observable behavior via public API, intent in the name
def test_get_instance_returns_none_for_unknown_id():
    """Guards against returning a stale instance for unregistered ids."""
    assert instance_manager.get_instance("nope") is None

# Bad — asserts an implementation detail, no intent
def test_get_instance_1():
    assert instance_manager.instances["x"]["state"] == "active"
```

## Test File Organization

Tests mirror the `motor/` directory structure:

``` text
motor/config/foo.py              → tests/config/test_foo.py
motor/controller/core/bar.py     → tests/controller/core/test_bar.py
```

If no test file exists, create one following `test_<module_name>.py`.

## Test Style Conventions

- **平铺函数，不用测试类**：新增测试一律写模块级 `def test_*` 函数，不新建 `class TestXxx`（既有文件已用类的，跟随该文件既有风格）。
- **不用 `# -- xxx ----` 章节分隔注释与多余空行**：函数之间标准 2 空行即可，分隔注释是多行视觉噪音。
- **不测纯日志输出**：日志文案（打印了哪个 banner、分隔符等）是实现细节，不是可观察行为——只断言通过公共 API 可观察的行为。
- **测试量随功能复杂度**：一个「小功能」不需要十几个用例；同类输入用 `@pytest.mark.parametrize` 合并，同函数多个分支优先合并到一个用例里断言（原则 4：测试 diff 明显大于代码 diff 就先砍）。

## Mock & Fixture Patterns

- **Mocking external services**: use `unittest.mock.patch` for HTTP clients and network calls
- **Fixtures**: use `pytest.fixture` for shared setup (config objects, mock clients)
- **Parametrization**: use `@pytest.mark.parametrize` for multiple input combinations
- **Thread safety**: ThreadSafeSingleton tests should verify single-instance behavior
- **Async tests**: use `pytest-asyncio` for coordinator ZMQ/scheduler tests

## Quick Reference: Testing by Module

```bash
bash tests/run_tests.sh tests/config/           # Config changes
bash tests/run_tests.sh tests/controller/       # Controller
bash tests/run_tests.sh tests/coordinator/      # Coordinator
bash tests/run_tests.sh tests/node_manager/     # NodeManager
bash tests/run_tests.sh tests/e2e/              # E2E integration
```
