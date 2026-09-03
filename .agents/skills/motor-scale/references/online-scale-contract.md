# Motor online scale contracts

Read only the selected profile section plus the shared assertion rules.

## Shared assertion rules

- Derive deadlines from live Coordinator/Controller convergence settings or ask
  the user to confirm one. Do not replace polling with a fixed sleep.
- Record the Coordinator log transition
  `Refresh instances done: E=…, P=…, D=…, U=…` when relevant, but correlate it
  with `/readiness`, Pod/process state, and inference.
- A profile passes only when pre-scale baseline, intended topology change, final
  functional state, and declared rollback (if any) all have evidence.
- Scaling success means native `deploy.py --update_instance_num` succeeded and
  the live cluster reflects the target counts.

## `prefill-scale-up-1-to-2`

Legacy reference: `pymotor_ras_up_down_scale_0001`.

### Preconditions

- Live deployment is healthy 1P1D (or profile-confirmed starting P count of 1).
- Native `user_config.json` is reachable at the deployment config directory.
- Baseline Coordinator `/readiness` is `ready=true`; controlled inference passes.
- Baseline Coordinator log or registration evidence shows `P=1` with the
  expected D count unchanged.

### Mutation plan

1. Copy or edit only the deployment's native `user_config.json` field
   `motor_deploy_config.p_instances_num` from `1` to `2`. Do not change unrelated
   fields in the same transaction unless the user explicitly requested them.
2. Run once from the Motor deployer directory:

   ```bash
   cd <motor-root>/examples/deployer
   python3 deploy.py --config_dir <remote-config-dir> --update_instance_num
   ```

3. Poll until: Coordinator `/readiness` returns HTTP 200 with `ready=true`;
   trustworthy evidence shows Prefill count = 2 while Decode count is unchanged;
   a Coordinator log line or equivalent registration evidence shows
   `Refresh instances done: ... P=2 ...` when logs are available.

### Required evidence

1. Before: config excerpt with `p_instances_num=1`, baseline topology, readiness,
   inference.
2. During: exact config diff, scaler command, stdout/stderr, exit status,
   intermediate Pod/workload changes.
3. After: config excerpt with `p_instances_num=2`, final topology, readiness,
   controlled inference success.
4. Rollback: not required for this profile unless the user separately authorizes
   downscale.

Primary source semantics: `examples/deployer/README.md` and
`handle_update_instance_num` in `deploy.py`.
