---
name: motor-reliability
description: Run authorized Motor reliability experiments for Coordinator failover, Decode engine-process restart, and Prefill NPU link-fault isolation/redundant recovery. Use for RAS, 高可用, 故障注入, 主备切换, 掉卡隔离, 进程重拉, or recovery validation. Read-only failure investigation belongs to motor-diagnosis.
---

# Motor reliability

Validate one explicitly selected recovery behavior against a live Motor
deployment. Real faults are injected only after a read-only plan and explicit
consent for the exact target. It does not deploy the service, edit its config,
or hide unsupported prerequisites.

Read `references/scenario-contracts.md` for the selected scenario only.

## Supported scenarios

| Scenario | Contract |
|---|---|
| Coordinator active/standby failover | `coordinator-failover` |
| Decode EngineServer process restart | `decode-engine-restart` |
| Prefill NPU parameter-plane link fault, isolation, and redundant recovery | `prefill-link-isolation` |

Online scaling, GPQA correctness, performance benchmarking, token-level replay,
and ScaleP2D are outside this Skill. Route scaling to `motor-scale`, the suite
GPQA contract to `motor-smoke-suite`, and explicit performance work to
`motor-validation-benchmark`; do not treat a recovery experiment as performance evidence.

## Common workflow

1. Resolve the current endpoint, kube context, namespace, native config,
   topology, component Pods, Services, endpoints, node/card mapping, and
   software revisions from live state, not an earlier deployment record.
2. Read the selected scenario contract and run all of its read-only preflight.
   Require `motor-validation-smoke` readiness and the smallest applicable
   `motor-validation-functional` inference check to pass before injection.
3. Establish a baseline with timestamps: Pod UIDs/restarts, process IDs and
   command lines when applicable, Coordinator role/endpoints, P/D/E/U topology,
   and one controlled inference result.
4. Present a fault transaction containing:
   - exact endpoint, context, namespace, Pod/container, node, process, NPU, or
     link target;
   - injection command and blast radius;
   - expected intermediate and recovered states;
   - bounded observation deadline derived from live config or confirmed by the
     user;
   - exact cleanup/restoration plan and evidence directory;
   - low-impact continuous probe rate, request shape, and allowed error budget;
   - stop conditions.
5. Authorization immediately before the mutation:
   - **Interactive:** obtain explicit consent. Consent covers exactly one
     injection and the mandatory restoration stated in the same transaction. A
     retry or different target requires new consent.
   - **Suite unattended:** when `motor-smoke-suite` passes suite
     pre-authorization with matching `profile_id`, exact target, and the
     scenario's `reliability:<scenario>` token in `allowed_actions`, do **not**
     re-prompt; out-of-scope scenario or target → `BLOCKED`. Restoration named
     in the same pre-authorized transaction is included; a new target or retry
     outside the profile still needs a new authorization path.
6. Start the declared time-correlated request probe and state observation before
   injection, inject once, and poll states rather than sleeping. Preserve
   requests, events, and logs from before, during, and after the transition.
7. Run the scenario's recovery assertions. A final healthy snapshot alone does
   not pass; the required transition and replacement/re-election evidence must
   also exist.
8. Execute mandatory cleanup/restoration even when an assertion fails or the
   observation deadline expires. If restoration fails, stop other work,
   prominently report the unresolved target, and request operator intervention.
9. On FAIL, use `motor-diagnosis` before any restart, redeploy, config edit, or
   second injection. Use a specialized diagnosis Skill only when its entry
   markers match.

## Safety boundaries

- Never select a target using only `grep | head -1`, a partial name, a stale
  Pod IP, or a guessed device index. Refuse ambiguous targets.
- Before killing a process, record its PID, parent PID, full command line,
  container, Pod UID, and owning role; recheck immediately before the signal
  and abort if PID identity changed.
- Before changing an NPU link, prove the target device/link belongs to the
  intended P instance and is not shared by unrelated workloads. Read the
  installed `hccn_tool` help and current link state; do not assume command
  syntax or that `up` restores the original state.
- Do not inject into multiple components, processes, cards, or links in one
  experiment.
- Do not auto-repair with rollout restart, Pod deletion, deploy, scaling, or
  config mutation. Only the predeclared restoration intrinsic to the injected
  fault may run without a second approval.
- The continuous probe is reliability evidence, not a throughput benchmark.
  Keep it low-impact, and report uninterrupted availability only when it
  covered the transition and its error budget was declared before injection.

## Evidence and result

Save raw evidence to a user-approved path or untracked
`.motor-local/reliability/<namespace>-<scenario>-<timestamp>/` (an artifact
directory, not a workflow gate). Include commands, UTC times, resolved targets,
redacted config facts, before/during/after snapshots, requests, events, logs,
cleanup results, and all assertion outcomes.

Report `PASS`, `FAIL`, `BLOCKED`, or `RESTORATION FAILED`. Distinguish the
injected fault, observed transition, recovery, request failures/timeouts and
longest interruption, user-visible availability, and cleanup. Never convert
missing transition evidence into PASS.
