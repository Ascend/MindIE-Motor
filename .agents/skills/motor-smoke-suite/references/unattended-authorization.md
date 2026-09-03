# Unattended authorization

Use this contract only for scheduled or CI suite runs. Interactive chat stays on
explicit per-run consent.

## Modes

| Mode | When | Consent |
|---|---|---|
| `interactive` | User asks in chat | Present the concrete mutation/load plan and obtain consent before live work |
| `unattended` | Outer scheduler or CI invokes a fixed entrypoint with an approved profile | Do **not** re-prompt. The approved profile is the authorization |

Never treat an ordinary “拉起一个服务 / 部署 / 扩缩容 / 注入故障” chat request as
`unattended`. Missing or invalid profile → `BLOCKED`, not silent interactive
fallback that mutates the cluster.

## Approved profile

Live approved profiles belong under the untracked path
`.motor-local/suite-profiles/<profile_id>.yaml`. Copy from
`references/profiles/unattended-suite.profile.example.yaml`, fill machine-specific
values, then set `approved_by` and `approved_at`. Do not commit approved profiles
with endpoint secrets or tokens.

`approved_by` / `approved_at` record human approval metadata, not a cryptographic
signature. Treat a profile as valid only when loaded from the expected local path,
fields validate, and `valid_until` has not expired.

Required fields: `mode=unattended`; `profile_id`; `approved_by` / `approved_at`
(human + UTC); optional `valid_until` (expired → `BLOCKED`); exact `endpoint` /
`kube_context` / `namespace`; `cases` in run order; exact `allowed_actions`;
logical `workload_intent` (model/parallel/topology/HA, not host paths);
endpoint-local `config.user_config_path` / `env_config_path` / `weight_mount_path`;
`baselines`; `software_revisions` when asserted.

`workload_intent` should match `references/suite-profiles.md` unless the approved
profile explicitly substitutes another model or parallel plan. Absolute paths
never belong in the Skill-owned intent table.

### Allowed action tokens

| Token | Covers |
|---|---|
| `deploy` | Native `deploy.py` apply for the profile namespace/config only |
| `delete_owned` | `delete.sh` / stop only for resources created by this run's namespace |
| `update_instance_num` | `motor-scale` transitions listed under `cases` |
| `reliability:coordinator-failover` / `reliability:decode-engine-restart` / `reliability:prefill-link-isolation` | That one reliability scenario each |
| `accuracy:gpqa` / `benchmark:gsm8k` | GPQA accuracy case / GSM8K performance case with profile baseline |

Any action not listed is out of scope. Broadening mid-run is forbidden.

## Suite behavior in `unattended`

1. Load and validate the profile before any mutation.
2. Resolve live endpoint/context/namespace/config/revisions. If live facts
   contradict the profile, stop with `BLOCKED`.
3. Record `profile_id`, approval metadata, and a content hash of the profile in
   the run report.
4. When routing to `motor-deploy`, `motor-deploy-k8s`, `motor-scale`, or
   `motor-reliability`, pass **suite pre-authorization**: mode `unattended`,
   `profile_id`, matched `allowed_actions` token(s), and exact target from the
   profile. Those Skills must not ask for new consent while the action stays
   inside that scope.
5. Deploy, readiness, inference probes, restoration, and cleanup remain fixtures
   or case assertions. They still need matching `allowed_actions`.
6. On failure: preserve evidence, run mandatory restoration/cleanup covered by
   the profile, then continue only when restoration is proved.

Scheduling is outside this Skill. A cron/CI/Automation should invoke one fixed
instruction: run `motor-smoke-suite` in `unattended` with the approved profile
path, do not ask for consent, report PASS/FAIL/BLOCKED/RESTORATION FAILED with
per-case evidence, and exit non-zero on non-PASS. Prefer a real Pymotor
automation entrypoint when available; the profile still defines its scope.
