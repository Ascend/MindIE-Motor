# [2026-11-17] Background Docker controller ignored SIGINT

- **Symptom**: An explicitly started continuous controller kept reconciling after SIGINT, although model requests still succeeded.
- **Root cause**: A background shell passed an ignored SIGINT disposition to Python. The default KeyboardInterrupt handler was not installed in that case (`examples/deployer/docker_cluster.py:main`).
- **Why**: Catching KeyboardInterrupt alone does not ensure that the process receives interrupt signals.
- **Fix**: Register SIGINT and SIGTERM for the optional `run` command. Both use the existing controller-only shutdown path; other commands and native model processes are unchanged.
- **Test interception**: Linux subprocesses inherit ignored SIGINT, receive either signal, exit with the controller-stopped response and release the writer lock. Non-continuous commands preserve their existing handlers.
- **Scenario**: Operators run the controller in a background shell or supervise it as a service.
- **Keywords**: Docker, orchestration, run, SIGINT, SIGTERM, background shell, writer lock.
