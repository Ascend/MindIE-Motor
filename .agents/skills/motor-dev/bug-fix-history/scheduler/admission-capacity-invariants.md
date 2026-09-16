# [2026-08-30] Program admission bypassed queue and stale capacity facts

- **现象 (Symptom)**：A later small Program could bypass an older queued Program; expired shared-prefix tokens could make a request appear to fit; max-segment rounds could resume a request without capacity.
- **根因 (Root cause)**：`scheduler/program.py` made direct-admission and tick decisions without queue precedence or prefix expiry before `_fits()`, and treated the max-round threshold as a capacity override.
- **为什么会写出 (Why)**：Fairness, cached-prefix freshness, and segment-yield policy were evaluated as separate concerns instead of being applied before the shared capacity decision.
- **修复 (Fix)**：New inactive Programs queue behind existing waiters, stale prefix state is refreshed before fit checks, privilege is assigned only after capacity fits, and only force-resume may bypass capacity.
- **测试拦截 (Test interception)**：`tests/coordinator/scheduler/test_program.py` covers queue precedence, stale prefix expiry, privilege capacity enforcement, and max-round capacity rejection.
- **场景 (Scenario)**：Progress-TTL is enabled while capacity is constrained and there are older waiting requests or expired cached prefixes.
- **关键词 (Keywords)**：scheduler, queue precedence, shared prefix, capacity, max segment
