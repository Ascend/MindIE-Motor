<!--
Copyright (c) 2026 ZhengDeL. All rights reserved.
MindIE is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
        http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details.
-->

# Agentless Docker orchestration example

This example maintains a desired number of complete Motor instances across explicitly approved Docker hosts.
It uses short-lived OpenSSH workers, the native Docker configuration and startup helpers, and the native
Controller, Coordinator and NodeManager APIs. It requires neither a Web console nor a resident host Agent.

The native Controller still registers, assembles and manages inference instances; the Coordinator routes
requests; NodeManager supervises engine processes. This tool adds host container placement, persistent resource
claims, reconciliation and optional metric-driven scaling. It does not change Motor's inference logic.

See [Design](DESIGN.md) for module responsibilities, durable operation recovery and resource safety boundaries.

## Supported scope

| Item | Scope |
| --- | --- |
| Hardware | `800I_A2` and `800I_A3` |
| Instances | Complete single-host Prefill/Decode instances, or complete single-host Union instances |
| Control plane | A separate native Coordinator/Controller container (`managed`), or an existing control plane (`external`) |
| Placement | Explicit candidate slots with physical NPU IDs; simultaneously selected instances must not overlap in devices or native ports |
| Recovery | Confirmed owned containers that exited abnormally; fresh admission is required before restarting |
| Scaling | Whole-instance manual scaling; optional bounded autoscaling from native running/waiting request metrics |

Multi-host *single* instances, Encode, Render, KV Store, KV Conductor orchestration and A5 are rejected in this initial example.
Union and P/D slots cannot be mixed. An instance is never split across arbitrary hosts to satisfy a placement.
This example does not provide control-plane high availability or lossless live migration.

## Prepare

1. Use Linux with Python 3.11+, OpenSSH and the native deployer's dependencies. Each target host needs Docker,
   `ss`, the Ascend driver and a checkout of the same compatible Motor revision. Choose a Motor/engine image
   compatible with that checkout. Preload the image on every target; the worker never pulls it automatically.
   Prefer an immutable `registry.example.org/motor@sha256:<digest>` image reference. Locally resolving a mutable
   tag to a content ID does not establish cross-host image consistency.
   A host may set `python_executable` to a trusted absolute Python 3.11+ executable, such as
   `/opt/motor-runtime/bin/python3`, without changing the system Python. It defaults to `python3` and selects
   the remote worker runtime, not the Python executable inside the Motor image.
2. Configure trusted SSH aliases and public-key authentication. Verify host keys through a trusted channel.
   Workers use `BatchMode=yes` and `StrictHostKeyChecking=yes`. The SSH account must be authorized to manage
   local Docker and the private work directory and host lock. This is trusted administrative code, not a sandbox.
3. Copy [cluster.example.json](cluster.example.json) to a private configuration directory. Replace its documentation
   addresses, SSH aliases, NIC names and absolute remote paths. `config_dir` is local to the controller and
   contains your native `user_config.json` and `env.json`; the worker receives those files over SSH.
4. Start from a compatible native [P/D configuration](../../../model_configs/vllm/user_config.json) and
   [environment](../../../model_configs/vllm/env.json). Set the image, weight paths, backend parallelism and
   communication settings for your hardware. For the supplied two-device slots, each P/D instance must fit one
   host: `single_p_instance_pod_num=1`, `single_d_instance_pod_num=1`, `p_pod_npu_num=2`, `d_pod_npu_num=2`.
   The full native validators still run; these four values alone are not a complete model configuration.
5. Reserve the selected devices exclusively for this deployment. Prevent Kubernetes or another scheduler from
   allocating them concurrently. Container declarations and a momentary idle sample cannot fence another scheduler.
   Ensure the full derived port ranges are available, not just the four explicit port starting points.

For `managed` P/D, add these settings to the corresponding existing sections of `user_config.json`:

```json
{
  "motor_coordinator_config": {
    "inference_workers_config": {"num_workers": 4, "worker_metaserver_base_port": 12000},
    "port_allocator_config": {"enable": false}
  },
  "motor_controller_config": {"port_allocator_config": {"enable": false}},
  "motor_engine_prefill_config": {
    "motor_nodemanger_config": {"port_allocator_config": {"enable": false}}
  },
  "motor_engine_decode_config": {
    "motor_nodemanger_config": {"port_allocator_config": {"enable": false}}
  }
}
```

Keep the native spelling `motor_nodemanger_config`. For Union, put the same NodeManager settings in
`motor_engine_union_config`. An `external` control plane requires the NodeManager settings for the selected
engines; its existing management processes remain externally managed. The managed example reserves the five
management/API listeners and worker ports `12000`–`12003`; a base of `0` disables the worker listeners.
The complete worker span must fit in `1`–`65535`. Native automatic port relocation is rejected because it
would invalidate the persisted resource ledger. The adapter preserves native fields and requires these
settings explicitly; it does not fill in missing native defaults.

The JSON example defines four candidate slots on two hosts, with an initial target of one P and one D.
Additional slots are capacity candidates, not pre-created instances. `managed` starts the native management
container on `control_plane.host_id`, with no NPU devices. For `external`, change `mode` to `external` and point
the addresses at an existing compatible Coordinator/Controller. `coordinator_url` is the **management API origin**,
not the inference or metrics URL. The managed management ports must also be free.
Alternative candidates may overlap; the resource ledger prevents simultaneous conflicting selection.
The shared Scheduler uses native IPC sockets, not additional TCP ports. This profile rejects non-empty
KV Conductor service settings, including the legacy and environment forms, rather than guessing an incomplete topology.

## Run and scale

Run these commands from the checkout root. Use the same config and persistent state directory throughout.

```bash
python3 examples/deployer/docker_cluster.py --config /srv/motor-demo/cluster.json --state-dir /srv/motor-demo/state plan
python3 examples/deployer/docker_cluster.py --config /srv/motor-demo/cluster.json --state-dir /srv/motor-demo/state apply --wait
python3 examples/deployer/docker_cluster.py --config /srv/motor-demo/cluster.json --state-dir /srv/motor-demo/state status
python3 examples/deployer/docker_cluster.py --config /srv/motor-demo/cluster.json --state-dir /srv/motor-demo/state scale --prefill 2 --decode 1 --wait
python3 examples/deployer/docker_cluster.py --config /srv/motor-demo/cluster.json --state-dir /srv/motor-demo/state scale --prefill 1 --decode 1 --wait
```

`plan` observes candidates without reserving resources or initializing a database. `status` reads persisted
state; it does not establish current model health. `apply --wait` reconciles until the desired instances are ready.
`scale` without `--wait` only saves desired counts; a later `apply` or running controller must reconcile them.

The preview is advisory: it checks explicit port starting points. The complete native-derived port set and final
resource admission are checked immediately before mutation. A preview is not deployment approval.

For continuous reconciliation, use `run` instead of repeated manual commands:

```bash
python3 examples/deployer/docker_cluster.py --config /srv/motor-demo/cluster.json --state-dir /srv/motor-demo/state run
```

Only one writer may own a state directory. Stop `run` before using mutating manual commands, then restart it
afterwards if needed. Stopping the controller process leaves model containers running. Keep the state on a
durable local filesystem; do not copy it to another controller while the original writer is active. Do not edit
the cluster or native configuration under active claims: the specification digest is bound to persisted state.

Optional autoscaling is disabled in the example. Before first deployment, configure `autoscaling.enabled=true`
and role-specific `min`, `max`, `up_running` and `down_running` limits. Only `run` enables automatic scaling.
Each decision requires `stable_samples` consecutive observations and a cooldown; missing or malformed metrics
never mean zero load. Thresholds use the sum of native running and waiting requests, not an NPU utilization target.
Enabled autoscaling requires a minimum of at least one instance per role; this example has no scale-from-zero
activation mechanism. An explicit manual `stop` may still set all desired counts to zero.

To stop the deployment, first stop the continuous controller, then run:

```bash
python3 examples/deployer/docker_cluster.py --config /srv/motor-demo/cluster.json --state-dir /srv/motor-demo/state stop
```

After all owned model instances are removed, `managed` also stops and removes the management container owned
by this deployment. `external` leaves the existing Coordinator/Controller untouched.

Scale-in pauses the selected instance through NodeManager, verifies that the Coordinator excludes it from
routing, checks the returned engine metric targets for zero running/waiting requests, and observes a grace
period before stopping it. Missing evidence or a timeout refuses removal and attempts to resume the instance.
An interrupted drain remains recorded and must be reconciled; it is not silently restored to Ready.

## Resource observations and failure handling

`observe_npu=true` explicitly permits bounded one-shot `npu-smi` queries for the configured physical IDs.
The worker verifies per-device physical/logical mapping and reads only those devices' health, processes,
utilization and HBM. It does not install a daemon, poll unapproved devices, or collect host CPU/OS monitoring data.
Missing commands or unsupported output remain unknown. Some HBM outputs provide a conservative lower bound,
not proof that a model will fit. Admission requires verified health, an empty process set, measured zero
utilization, positive free HBM, free ports and no competing container declarations.
An active privileged container is conservatively treated as able to access every selected device, even if
Docker lists no individual device mappings. The worker does not exempt containers by their display name.

Alternatively, `npu_observation_file` can name a root-controlled schema-2 snapshot. Its host identity, same-boot
monotonic timestamp, expiry, physical mapping and process scope must be verifiable. A valid snapshot takes
precedence; one-shot sampling is only a fallback when explicitly enabled. See
[`_snapshot_devices`](../lib/docker_orchestration/native.py) for the bounded snapshot contract and
[`sample_devices`](../lib/docker_orchestration/observations.py) for the independent producer.

Each host operation takes a host-wide lock and records intent before contacting Docker. Exact container IDs,
generation, specification and ownership labels are checked before lifecycle operations. A lost SSH connection
does not prove that creation failed: the resource claim is retained and the persistent operation receipt is
queried before any retry. An unreachable host blocks replacement. Restore trustworthy observation or establish
external fencing before recovery; the example does not implement fencing or automatic migration after a partition.

Abnormally exited owned containers, including confirmed OOM exits, can be restarted within retry limits.
Normal exits (`0` or `143`) without an explicit OOM indication are not automatically restarted.
A running container with an unreachable NodeManager
is not considered dead. `restart --slot <slot-id>` requests an explicit restart of a confirmed terminal owned
container and repeats final resource admission; it does not bypass unknown hardware observations.

## State, logs and transport

Controller state defaults to `~/.local/state/mindie-motor/<cluster>/state.sqlite3`; `--state-dir` overrides it.
On each host, receipts and private instance workspaces live under
`<work_dir>/.docker-orchestration/<cluster>/`. Native run logs are written under the instance workspace's
`log/` directory. Docker also retains container output according to its configured logging driver. Retain
state and receipts when diagnosing unknown operations; deleting a database is not a recovery procedure.

Keep configuration, state and TLS material private. Native JSON fields are preserved. The `tls` object can
provide controller-local `ca_file`, `cert_file` and `key_file`; `management_headers` provides required management
headers. Native TLS configuration and actual management URLs must agree. No anonymous management service or
new host TCP listener is added.

This directory and its JSON example are licensed under [Mulan PSL v2](../../../LICENSE.md).
