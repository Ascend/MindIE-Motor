---
name: motor-scale
description: Run authorized online Prefill instance scale-up against a live Motor deployment via native deploy.py --update_instance_num. Use for 在线扩容, P instance scale-up (1→2), update_instance_num, or pymotor_ras_up_down_scale_0001-style validation. 缩容 / scale down is not supported and will be BLOCKED. Read-only status belongs to motor-deploy-k8s; full suite orchestration belongs to motor-smoke-suite.
---

# Motor scale

Validate one explicitly selected online scaling transition against a live Motor
deployment. This Skill mutates instance counts only through the native deployer;
it does not deploy from scratch, edit unrelated config, or inject faults.

Read `references/online-scale-contract.md` for the selected profile only.

## Supported transitions

| Profile | Contract |
|---|---|
| Prefill scale-up (legacy 1P→2P) | `prefill-scale-up-1-to-2` |

Prefill scale-down, Decode-only scaling, hybrid/U scaling, and ScaleP2D are
outside this Skill unless a new contract is added. Route deployment creation to
`motor-deploy`; route fault injection to `motor-reliability`.

## Common workflow

1. Resolve endpoint, kube context, namespace/job ID, native config directory,
   current live topology, Coordinator Services, and software revisions from live
   state. Do not reuse stale deployment records.
2. Read the selected profile contract and run all read-only preflight. Require
   `motor-validation-smoke` readiness and the smallest applicable
   `motor-validation-functional` inference check before scaling.
3. Establish a baseline with timestamps: Pod UIDs/restarts, P/D/E/U topology from
   Coordinator logs and live registration, `/readiness`, and one controlled
   inference result.
4. Present a scaling transaction containing:
   - exact config field changes (`p_instances_num`, `d_instances_num`, or other
     profile-declared fields);
   - exact `deploy.py --update_instance_num` command and config directory;
   - expected intermediate and final topology;
   - bounded observation deadline derived from live config or confirmed by the
     user;
   - evidence directory;
   - rollback plan when the profile defines one.
5. Authorization immediately before config mutation and
   `--update_instance_num`:
   - **Interactive:** obtain explicit consent. Consent covers exactly one
     scaling transition.
   - **Suite unattended:** when `motor-smoke-suite` passes suite
     pre-authorization with matching `profile_id`, exact target
     namespace/config, and `allowed_actions` containing `update_instance_num`,
     do **not** re-prompt; out-of-scope transition → `BLOCKED`.
6. Apply the config change, run the native scaler once, and poll convergence
   rather than sleeping. Correlate Coordinator log transitions
   `Refresh instances done: E=…, P=…, D=…, U=…` with `/readiness`, Pod state,
   and inference; do not treat the log line as sole proof.
7. Run the profile's post-scale assertions, including controlled inference after
   the target topology is observed.
8. On FAIL, use `motor-diagnosis` before retry, rollback, redeploy, or a second
   scale attempt.

## Safety boundaries

- Never scale when baseline topology, readiness, or inference does not match the
  profile's starting state.
- Never use `--update_config` to change instance counts; use
  `--update_instance_num` only.
- Do not combine scaling with fault injection, wheel replacement, or unrelated
  config edits in one transaction.
- Do not delete Pods or run rollout restart as a substitute for native scaling.
- Downscale is not implicit cleanup; treat it as a separate profile with its own
  interactive consent or suite `allowed_actions` token when added later.

## Evidence and result

Save raw evidence to a user-approved path or untracked
`.motor-local/scale/<namespace>-<profile>-<timestamp>/`. Include
commands, UTC times, config diffs, before/during/after topology snapshots,
readiness polls, inference results, and assertion outcomes.

Report `PASS`, `FAIL`, `BLOCKED`, or `ROLLBACK FAILED`. Never report PASS when
the target topology or post-scale inference evidence is missing.
