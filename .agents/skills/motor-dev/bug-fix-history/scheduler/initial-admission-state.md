# [2026-08-22] New Programs were incorrectly queued as paused

- **现象 (Symptom)**：A capacity-fitting first request returned `queued` instead of `admitted`.
- **根因 (Root cause)**：The new Program record is materialized as `PAUSED`; admission checked `state == ACTIVE` before testing capacity.
- **为什么会写出 (Why)**：The state transition table was applied to resume paths but not distinguished from first materialization.
- **修复 (Fix)**：Capacity-fit admission is now evaluated for newly materialized and paused Programs alike.
- **测试拦截 (Test interception)**：`tests/coordinator/scheduler/test_program.py` and the standalone Program smoke check assert first-request admission.
- **场景 (Scenario)**：Progress-TTL enabled with a new session and available logical KV capacity.
- **关键词 (Keywords)**：scheduler, program, admission, paused, capacity
