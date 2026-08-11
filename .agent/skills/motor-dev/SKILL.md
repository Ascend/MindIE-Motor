---
name: motor-dev
description: MindIE-PyMotor development guidelines. Use when modifying motor/ or tests/ code, especially config or Controller module changes.
---

# MindIE-PyMotor Development

## Continuous Learning (可持续学习)

This skill is **self-improving**. Every debugging session adds a case to the bug-fix history —
the "why did I write this bug" lesson plus the test that intercepts it — so future development
benefits from past mistakes.

- **Always check `bug-fix-history/INDEX.md` FIRST** when debugging, fixing, or writing tests —
  it is a lightweight one-line-per-case index. Match by keyword/module, then read only the
  matching case file under `bug-fix-history/<module>/` (progressive disclosure — do not load the
  whole history).
- **Always record** after a bug-fix loop completes (tests green, verified): create a case file
  under `bug-fix-history/<module>/` following the template in `INDEX.md`, then add one row to
  `INDEX.md` (dedup by keyword, max 10 cases per module).
- Record only **verified** conclusions — never guesses. Skip typos/environment-only issues.
- The same rules apply to user-provided logs: diagnose → confirm → fix + test interception → record.

## Hard Constraints

- Every `motor/` change adding new logic (functions, methods, classes, parameters, branches) **must** include corresponding tests.
- Always use `bash tests/run_tests.sh` — never `python -m pytest` directly.
- Never use f-strings in logger calls — use `%s`, `%d` for deferred formatting.
- Every Python file must start with the Mulan PSL v2 license header (see `references/code-style.md`).
- After any completed bug fix (including log-diagnosed issues), **record the case in `bug-fix-history/`** (case file + INDEX.md row) — this is mandatory, not optional (see Continuous Learning).
- **Skill sync (docs never drift)**: whenever you read component source code while developing, actively cross-check it against the corresponding `references/<module>.md`. If reality differs from the doc (path/line counts/constants/state machine/protocol/flow), **update the reference immediately** and carry it in the same PR as the code change. Never leave a known mismatch in place.
- **References are development knowledge, not verification reports**: `references/<module>.md` records only what later development needs to know (architecture, mechanisms, wire protocols, constants, test/benchmark entry points and how to run them). **Never write one-off verification results — benchmark performance numbers, single-run tuning conclusions, measured gains — into skill references**; they go stale as hardware/code evolve and belong in PR/ISSUE bodies or user-facing docs. Keep the mechanism, drop the numbers.

## Debugging Workflow (含日志定位)

Follow this loop whenever debugging or fixing bugs — it closes the learning cycle:

1. **Search first** — read `bug-fix-history/INDEX.md`, match by keyword/module, then read the matching case file; reuse the fix and test interception if one exists.
2. **Reproduce / diagnose** — for user-provided logs: read the log, trace the code path, confirm the root cause before touching code.
3. **Fix + write the intercepting test** — the test must fail on the old code and pass on the new code (regression guard).
4. **Verify** — run the targeted test, then the module tests, via `bash tests/run_tests.sh`.
5. **Record** — create `bug-fix-history/<module>/<short-name>.md` using the template (Symptom / Root cause / Why / Fix / Test interception / Scenario / Keywords) and add one row to `bug-fix-history/INDEX.md`. Dedup by keyword; skip typos and environment-only issues.
6. **Offer to file an ISSUE (+PR)** — after recording the case, ask the user whether to file a bug report against the Motor repo (gitcode.com/Ascend/MindIE-Motor, `bug-report` template). Do **not** load `references/issue-reporting.md` unless the user agrees — it is only read on demand (progressive disclosure). If they agree, follow that reference. **顺序硬性要求：先提 ISSUE，拿到编号后再提 PR，PR 正文直接写 ISSUE 链接**；提交方式：token-based API 提交 vs. markdown 草稿 vs. skip。

## Testing (Core Principles)

### Progressive Testing Workflow

**Never run the full suite first.** Start small, expand gradually:

1. **Single test file** — the one you added or modified:

   ```bash
   bash tests/run_tests.sh tests/path/to/specific_test.py
   ```

2. **Module-level** — all tests in that directory:

   ```bash
   bash tests/run_tests.sh tests/module_name/
   ```

3. **Full suite** — only when all module tests pass:

   ```bash
   bash tests/run_tests.sh
   ```

### What to Cover

When adding tests, cover: normal case (happy path), edge cases, error/failure handling, and any new parameter variations. Follow existing test patterns in the matching test file (mocking, fixtures, parametrization).

### Test Design Principles

1. **Design before you write** — answer four questions first: what is the module for, what is its I/O contract, what failure am I guarding against, and what is the cheapest level that catches it (unit > integration > e2e)?
2. **Reuse before create** — extend existing test files, `conftest.py` fixtures, and helpers; add a new file only when no nearby suite fits.
3. **Test behavior with intent** — assert observable outcomes through public APIs; state why in the name or docstring. Skip trivial wiring; flaky tests are worse than no tests.
4. **Keep it minimal** — one behavior per test, smallest setup that triggers it; if the test diff dwarfs the code change, cut scope.

For test script options, E2E testing, and detailed testing patterns, read `references/testing-guide.md`.

## Code Style (Summary)

- **License header**: Mulan PSL v2 — must be the first thing in every Python file
- **Logging**: `logger.info("msg %s", var)` — never f-strings
- **Line length**: max 120 chars (ruff)
- **Indentation**: 4 spaces, no tabs
- **Class member order**: `__init__` → `@staticmethod` → `@classmethod` → public → private → `@property`
- **Imports**: stdlib → third-party → local (blank line between groups)
- **Naming**: PascalCase classes, snake_case functions, UPPER_SNAKE_CASE constants
- **Type hints**: required on all parameters and return values; **Python 3.10+ native syntax only** — `X | None` not `Optional[X]`, `dict[K, V]` not `Dict[K, V]` (pre-commit `check-modern-typing` enforces this; `Any` is the only common `typing` import)
- **Docstrings**: triple double-quotes, Google/NumPy style

For complete code style guide with examples, read `references/code-style.md`. Run `pre-commit` locally before pushing — ruff, pylint, bandit, `check-header`, and `check-modern-typing` are the authoritative style gatekeepers.

## Module Map — Which Reference to Read

The skill body only covers core workflow. **When touching a specific module, read the corresponding reference:**

| When modifying... | Read this reference |
|-------------------|---------------------|
| `motor/config/` | (inline below — Config Changes) |
| `motor/controller/` | `references/controller.md` |
| `motor/coordinator/` | `references/coordinator.md` |
| `motor/coordinator/metrics/` | `references/metrics.md` |
| `motor/node_manager/` | `references/nodeman.md` |
| `motor/engine_server/` | `references/engine-server.md` |
| `tests/e2e/` | `references/e2e-testing.md` |
| `motor/kv_conductor/` | `references/kv-conductor.md` |
| General testing / test options | `references/testing-guide.md` |

Each reference includes: architecture overview, key files table, development rules, and testing commands.

## Skill Sync — 开发中文档同步检查（铁律落地）

While developing, the source code you read is the **ground truth** — the references are a distilled view that must never contradict it. Whenever you read a component's code, cross-check the corresponding reference:

| 检查项 | 发现出入时 |
|--------|-----------|
| 文件路径 / 行数引用 | 更新 Key Files 表 / 正文引用 |
| 常量值（超时、阈值、magic、枚举） | 更新描述（附新值 + 源码位置） |
| 状态机 / 协议 / 事件流 | 更新对应章节 |
| 模块新增 / 删除 / 职责变化 | 更新架构图与 Key Files |
| 测试命令 / 端口约定 | 更新 Testing / Port 章节 |

**规则**：更新必须与代码改动**同一个 PR** 合入（skill 与代码同步演进）；发现 mismatch 但本次改动不涉及该模块时，也要在本 PR 顺手修正或在 PR 描述中说明。禁止「知道了但没改」。

## Config Changes

When modifying files in `motor/config/`:

1. Check **all** `user_config.json` files under `examples/` — keep them consistent with the new config schema.
2. Run config tests:

   ```bash
   bash tests/run_tests.sh tests/config/
   ```

3. If tests fail, update test expectations to match the new config. Re-run until passing.

## Quick Reference: Testing by Module

```bash
# Config
bash tests/run_tests.sh tests/config/

# Controller
bash tests/run_tests.sh tests/controller/

# Coordinator
bash tests/run_tests.sh tests/coordinator/

# NodeManager
bash tests/run_tests.sh tests/node_manager/

# EngineServer
bash tests/run_tests.sh tests/engine_server/

# E2E
bash tests/run_tests.sh tests/e2e/
```
