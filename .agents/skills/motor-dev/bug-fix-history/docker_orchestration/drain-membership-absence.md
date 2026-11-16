# [2026-11-17] Native pause omitted a job from the instance view and blocked draining

- **Symptom**: A P/D maintenance stop timed out despite an acknowledged pause and idle engines. The target job was absent from the Coordinator view, so the adapter retained its resources and resumed it.
- **Root cause**: `NativeManagement.drain` required a persistent `paused` row. Native lifecycle views can omit a paused or unregistered job; available/unavailable snapshots and all-tracked snapshots differ.
- **Why**: The adapter treated one instance-view representation as a universal pause protocol.
- **Fix**: Require a complete count-consistent view with named jobs. After pause succeeds, either explicit paused membership or absence establishes routing exclusion. Engine quiescence and the grace period remain mandatory; only exact Docker stop/remove receipts release resources.
- **Test interception**: Omitted targets and explicit paused rows drain successfully; missing or inconsistent counts, unnamed jobs, failed pause, incomplete metrics and busy engines refuse removal. Real P/D inference still succeeded after timeout/resume, and the corrected maintenance stop converged.
- **Scenario**: Whole-instance P/D maintenance after ingress and in-flight KV transfers are externally quiesced. This does not enable automatic P/D scale-in.
- **Keywords**: Docker, drain, pause, instance view, count, P/D.
