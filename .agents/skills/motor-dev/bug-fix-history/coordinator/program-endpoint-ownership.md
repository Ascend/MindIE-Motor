# [2026-08-30] Program allocation switched endpoint after admission

- **现象 (Symptom)**：A request admitted against one DP endpoint could allocate another endpoint after topology refresh; a queued poll without a capacity probe read an uninitialized local variable.
- **根因 (Root cause)**：`scheduler_client.py` fell back from an exact endpoint pin, while `router/strategies/base.py` assigned `capacity_result` only when the optional probe existed.
- **为什么会写出 (Why)**：Best-effort routing fallback was applied to a capacity-owned Program path, and optional-probe handling did not preserve initialized fallback facts.
- **修复 (Fix)**：Unavailable exact endpoint pins fail capacity probing and allocation without moving to another DP endpoint, polling without a probe keeps configured fallback capacity, and legacy two-value probes still refresh capacity.
- **测试拦截 (Test interception)**：`test_scheduler_client.py` rejects a missing pinned endpoint; `test_base_router_prepare_resource.py` polls safely without a probe.
- **场景 (Scenario)**：An admitted Program’s DP endpoint disappears or capacity probing is unavailable during a queued admission wait.
- **关键词 (Keywords)**：scheduler client, endpoint pin, capacity probe, Program admission, DP
