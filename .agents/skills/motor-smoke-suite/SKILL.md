---
name: motor-smoke-suite
description: Define the repeatable Pymotor cluster validation suite for online scale, RAS recovery, GPQA accuracy, and performance. Use for Pymotor 冒烟套组, 全量验收, full acceptance, nightly suite, or cluster-level e2e beyond tests/e2e. Minimal Coordinator readiness alone is not this Skill.
---

# Motor smoke suite

Canonical **acceptance contract** for the Pymotor cluster validation suite in
this repository: cases, workload intent, pass rules, and unattended
authorization. It does not invent a second deployer or a generic test harness.

Prefer native `examples/deployer/` (`deploy.py` / `delete.sh`) and the owning
atomic Skills under `.agents/skills/` when no separate automation suite is
supplied.

Read [references/suite-profiles.md](references/suite-profiles.md) before
planning or running the suite,
[references/unattended-authorization.md](references/unattended-authorization.md)
before any scheduled/CI run, and
[references/failure-routing.md](references/failure-routing.md) only after a case
fails or blocks.

## Owning Skills

| Case area | Skill |
|---|---|
| Deploy / stop owned resources | [`motor-deploy`](../motor-deploy/SKILL.md) → [`motor-deploy-k8s`](../motor-deploy-k8s/SKILL.md) |
| Online scale | [`motor-scale`](../motor-scale/SKILL.md) |
| RAS / reliability | [`motor-reliability`](../motor-reliability/SKILL.md) |
| GPQA accuracy | 本 Skill 的 `references/gpqa-profile.md`（执行路由到 `motor-validation-accuracy`） |
| GSM8K performance | [`motor-validation-benchmark`](../motor-validation-benchmark/SKILL.md) |
| Readiness / inference probes | [`motor-validation`](../motor-validation/SKILL.md) |
| Failure investigation | [`motor-diagnosis`](../motor-diagnosis/SKILL.md) |

## Execution source

1. Prefer a real Pymotor automation suite when its source and entrypoint are
   supplied — setup, case body, restoration, and teardown remain authoritative.
2. Otherwise route each selected case to the owning Skill above and reuse native
   deployer commands plus that Skill's contract.
3. Do not claim a code-driven suite run from documentation alone when required
   tools, datasets, or baselines are absent on the live endpoint.

## Modes

| Mode | Default trigger | Authorization |
|---|---|---|
| `interactive` | Chat request for the suite | Present the full concrete mutation/load plan and obtain consent before live work |
| `unattended` | Fixed outer entrypoint + approved profile | Do not re-prompt; enforce the profile scope or `BLOCKED` |

Ordinary deploy/scale/RAS chat requests are never `unattended`.

## Suite contract

1. The full suite means the six concrete cases in
   `references/suite-profiles.md`. A user may select a subset.
2. Service creation, readiness, inference probes, restoration, and cleanup are
   shared setup/teardown or case assertions — not separate suite stages.
3. Use the **Reference workload intent** in `suite-profiles.md` as the logical
   target (model family, served name, P/D topology, TP/DP/EP, HA). Resolve host
   paths, mounts, and image tags on the live endpoint; do not bake absolute
   addresses into this Skill.
4. Resolve target, native configuration, case inputs, thresholds, timeouts, and
   software revisions before starting. Do not invent missing paths, baselines,
   datasets, or targets.
5. **Interactive:** present the complete concrete mutation/load plan for
   authorization before a live run. Execution does not broaden that
   authorization.
6. **Unattended:** load an approved profile (see
   `references/unattended-authorization.md`), validate it, then run without
   further consent. Pass suite pre-authorization into
   `motor-deploy` / `motor-deploy-k8s` / `motor-scale` / `motor-reliability`
   for in-scope actions only.
7. Preserve each case's raw logs and metrics. Determine PASS/FAIL from owning
   Skill contracts or automation code, never from Agent prose.
8. After a failure, preserve evidence and run mandatory restoration/cleanup.
   Continue to an independent case only when the target is proven restored;
   otherwise stop and report `RESTORATION FAILED`.

## Scheduled entrypoint

Scheduling is an outer concern (cron, CI, or Automation). The fixed invocation
must name `unattended` mode and the profile path. Prefer one real automation
entrypoint with a versioned configuration when available. Publish ordinary
logs, metrics, summary, and a non-zero status on non-`PASS`.

Report selected cases, mode, `profile_id` (when unattended), resolved
target/configuration, code and tool revisions, per-case result and evidence,
cleanup status, and overall `PASS`, `FAIL`, `BLOCKED`, or `RESTORATION FAILED`.
