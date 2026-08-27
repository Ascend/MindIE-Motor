# KV Conductor — Architecture, Implementation & Development

## Overview

KV Conductor is a standalone Rust HTTP service (axum + tokio) that maintains **radix prefix trees** of cached KV blocks, indexed per `(model_name, tenant_id)` pair. It answers KV cache overlap queries from routers/schedulers, enabling **cache-aware request routing** — steering requests toward the worker that already has the longest matching token prefix cached.

Replaces Mooncake conductor for MindIE-PyMotor. Design priorities:

- **Low-latency queries**: O(path_length) radix tree traversal with `parking_lot::RwLock` read locks — multiple concurrent queries don't block each other.
- **Per-tenant isolation**: Each `(model, tenant)` pair gets its own indexer entry.
- **Multi-tier storage awareness**: XPU/CPU/DISK tracked independently per block with configurable weights.
- **Push-based ingestion**: Events from vLLM engines via ZMQ SUB, HTTP `POST /events`, or Mooncake pool backends.

### References

- KV Event wire format & multi-tier storage model: [Mooncake RFC #1527](https://github.com/kvcache-ai/Mooncake/issues/1527)
- Radix tree design: [NVIDIA Dynamo kv-router](https://github.com/ai-dynamo/dynamo/tree/main/lib/kv-router)

## Architecture

``` text
┌─────────────────────────────────────────────────────────┐
│                  KV Conductor Service                    │
│                   (axum 0.7 / tokio)                     │
│                                                         │
│  ┌──────────────┐  register   ┌──────────────────────┐ │
│  │   Engine     │────────────►│   WorkerRegistry      │ │
│  │  (vLLM/      │◄───────────│     instances:         │ │
│  │   SGLang)    │   query     │      RwLock<HashMap>   │ │
│  └──────────────┘             │     indexer: Arc<>     │ │
│                               │     zmq_subscribers    │ │
│  ┌──────────────┐  events     │     hbm_ip_index       │ │
│  │  Mooncake    │────────────►│                        │ │
│  │  master (ZMQ)│  (HTTP or   └───────────┬────────────┘ │
│  └──────────────┘   ZMQ SUB)              │              │
│                                           ▼              │
│  ┌──────────────┐  GET        ┌──────────────────────┐ │
│  │  Router/     │ /health     │   Indexer             │ │
│  │  Scheduler   │ /workers    │   DashMap<(model,     │ │
│  └──────────────┘             │     tenant)→Entry>    │ │
│                               │                       │ │
│                               │  Per Entry:           │ │
│                               │   - hbm_tree (Radix)  │ │
│                               │   - cpu_blocks (Flat) │ │
│                               │   - disk_blocks (Flat)│ │
│                               │   - offload_pool_state│ │
│                               └───────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### HTTP Endpoints

| Endpoint | Method | Purpose |
|------|------|
| `/register` | POST | Register a worker instance (HBM or pool) |
| `/unregister` | POST | Remove worker; cleans up radix-tree blocks |
| `/query` | POST | Query KV cache overlap scores for token sequence |
| `/query_by_hash` | POST | Query using pre-computed `LocalBlockHash` values |
| `/events` | POST | Ingest KV cache events (store/remove/clear) via HTTP |
| `/health` | GET | Liveness check, returns `"OK"` |
| `/workers` | GET | Debug: all registered workers + indexer summary |

Both `/query` and `/query_by_hash` accept **JSON (default) and MessagePack**
(`Content-Type: application/msgpack` / `application/x-msgpack`) request bodies;
the response is returned in the request's encoding. See
[MessagePack Query Codec](#messagepack-query-codec) below.

### HTTP `/events` Protocol (`KvEventBatch`)

Body fields:

- `instance_id` (required) — the worker the events originate from
- `model_name`, `tenant_id` (optional) — fall back to the registered values when omitted
- `block_size` (optional) — falls back to the registered value, then to the default `128`
- `events` — list of `KvCacheEvent`
- `shutdown` (optional bool) — shutdown flag for the instance

Each `KvCacheEvent` accepts two JSON shapes via serde aliases:

- **Nested**: `{"event_id": 1, "data": {"type": "stored", ...}, "dp_rank": 0}`
- **Flat**: `{"event_id": 1, "type": "stored", ..., "dp_rank": 0}`

---

## Module Map (~7500 lines Rust)

Crate root: `motor/kv_conductor/` (paths below are relative to it).

| File | Role |
|------|------|
| `src/main.rs` | CLI entry: host/port, tracing (UTC+8), axum serve |
| `src/lib.rs` | Module declarations + re-exports |
| `src/server.rs` | HTTP routes, `AppState { registry }`, middleware, JSON/msgpack content negotiation on query endpoints |
| `src/registry.rs` | WorkerRegistry: register/unregister/query dispatch, ZMQ lifecycle, re-registration, replay gating |
| `src/indexer/` | Indexer (DashMap), IndexerEntry (hbm_tree + cpu/disk flat + offload cache), query, two-phase matching (`mod.rs`, `tests.rs`) |
| `src/concurrent_tree.rs` | ConcurrentRadixTree (`Arc<RwLock<Block>>`), find_matches/apply_store/remove_worker |
| `src/backend.rs` | StoreBackend enum + MatchMode, IP→DP resolution |
| `src/zmq_subscriber.rs` | ZMQ SUB socket I/O, 3-format payload dispatch, reconnect loop, replay DEALER→ROUTER |
| `src/events/mod.rs` | Event module root: re-exports, attention-group filtering docs |
| `src/events/vllm.rs` | VllmEventMap parsing + `apply_vllm_event()` + `parse_vllm_batch()` |
| `src/events/pool.rs` | PoolEvent / MemcacheEventBatch parsing + `apply_pool_event()` |
| `src/events/flex_hash.rs` | FlexHash: polymorphic u64 deserializer (int/binary/string) |
| `src/events/helpers.rs` | `resolve_medium()`, `resolve_workers()` |
| `src/events/tests.rs` | Unit tests for event parsing |
| `src/protocols.rs` | HTTP types, WorkerKey, StorageMedium, ScoringConfig, KvCacheEventData, query/response types |
| `src/hashing.rs` | `compute_block_hash_for_seq()` — XXH3 with rayon parallel |
| `src/error.rs` | KvConductorError (thiserror) |
| `tests/integration_test.rs` | HTTP API integration tests (axum test server, 17 tests) |

---

## Core Data Structures

### Hash Types

Two distinct hash types serve different purposes:

| Hash | Source | Purpose | Tree Role |
|------|------|-----------|
| `LocalBlockHash(u64)` | XXH3 of token bytes in a block | Content-addressed radix tree key | Primary — determines tree position |
| `SequenceBlockHash(u64)` | Engine-provided rolling hash (includes parent context) | Reverse lookup by engine sequence hash | Secondary — stored in `Block.block_hash` for O(1) removal |

`block_size` is passed by the caller per-query, not stored in the tree. Hashes computed at different `block_size` values coexist safely — they are distinct `u64` values. **The Coordinator must use the same `block_size` the engine uses for KV event publishing.**

### WorkerKey

```rust
pub struct WorkerKey {
    pub instance_id: String,   // e.g. "prefill-0"
    pub backend_id: String,    // may differ from instance_id for pool backends
    pub dp_rank: u32,          // data-parallel rank
    pub medium: StorageMedium, // Xpu / Cpu / Disk
}
```

`backend_id` follows RFC #1527 — it may differ from `instance_id` when blocks originate from a Mooncake daemon rather than the engine itself.

### StorageMedium (RFC #1527)

| Enum | Wire Value | Source | Typical Medium |
|------|------|---------------|
| `Xpu` | `"xpu"`, `"hbm"`, `"device"` | Engine worker events | GPU/NPU HBM |
| `Cpu` | `"cpu"`, `"host"`, `"memory"` | Pool backend MEMORY replica | Host DDR |
| `Disk` | `"disk"`, `"ssd"`, `"nvme"`, `"dfs"` | Pool backend DISK replica | SSD/NVMe/DFS |
| `Unknown` | anything else (e.g. `"unknown"`) | `StorageMedium::parse()` fallback | 4th distinct variant (not folded into Xpu) |

---

## Multi-Medium Indexing: HBM Tree vs CPU/Disk Flat Maps

This is the core architectural decision. Different storage media use **different data structures** because their access patterns differ.

### HBM/XPU: ConcurrentRadixTree (Prefix Chain)

**Why a tree:** KV cache reuse depends on **contiguous prefix matching**. If a request shares the first 384 tokens with a cached sequence, the router should route to the worker that has those 384 blocks cached. A prefix tree enables O(L) traversal along the query sequence, discovering per-worker match depth at each level.

**Tree structure:**

``` text
root (no block_hash)
├─[LocalBlockHash(0xA)]── Block { workers: {W1, W2}, children: ... }
│  ├─[LocalBlockHash(0xB)]── Block { workers: {W1}, children: ... }
│  │  └─[LocalBlockHash(0xC)]── Block { workers: {W1} }
│  └─[LocalBlockHash(0xD)]── Block { workers: {W2} }
└─[LocalBlockHash(0xE)]── Block { workers: {W3} }
```

Each `Block` node:

- `children: FxHashMap<LocalBlockHash, SharedBlock>` — keyed by token-content hash
- `workers: Arc<FxHashSet<WorkerKey>>` — which workers have this block cached (Arc enables CoW for fast clone on query path)
- `block_hash: Option<SequenceBlockHash>` — for reverse lookup

**Concurrency:**

- `find_matches()`: read locks only — multiple concurrent queries proceed simultaneously
- `apply_store()`/`apply_remove()`: hand-over-hand write locks (parent → child → release parent), plus external lookup table write lock
- Worker set uses `Arc::make_mut` — mutations clone-on-write, queries only bump refcounts

**Memory reclamation:** When the last worker is removed from a block, `drop_worker()` clears `self.children` so the subtree is dropped. Orphan nodes (from worker disconnection) are cleaned up by `sweep_stale_nodes()` triggered every 1000 HBM removals.

### CPU/Disk: FxHashMap (Flat)

**Why flat:** Pool blocks (CPU/Disk) are isolated single blocks — there is no chain relationship between consecutive blocks. The only question is "does any worker have this block cached?" A flat `tokens_hash → {workers}` map suffices and avoids tree overhead.

```rust
cpu_blocks:  Arc<RwLock<FxHashMap<LocalBlockHash, FxHashSet<WorkerKey>>>>
disk_blocks: Arc<RwLock<FxHashMap<LocalBlockHash, FxHashSet<WorkerKey>>>>
```

**Parallel optimization:** When query hashes exceed `FLAT_PAR_THRESHOLD` (4096), CPU and Disk lookups run in parallel via `rayon::join`.

### Scoring Model

Per DP, Conductor exclusive-partitions absolute coverage ends (NPU > CPU > Disk):

``` text
npu_blocks  = npu_end
cpu_blocks  = max(0, cpu_end - npu_end)
disk_blocks = max(0, disk_end - max(npu_end, cpu_end))
matched_tokens = (npu + cpu + disk) × block_size   # unweighted coverage
longest_matched = max(matched_tokens over DP ranks)
```

The conductor reports **raw coverage**, not weighted scores — the old
`--hbm-weight/--cpu-weight/--disk-weight` CLI flags and `total_score`
response field no longer exist. Coordinator `kv_cache_affinity` applies tier
weights when ranking:

``` text
affinity_matched = round((npu×w_npu + cpu×w_cpu + disk×w_disk) × block_size)
```

Defaults in `SchedulerConfig.kv_affinity`: `w_npu=1.0`, `w_cpu=1.0`,
`w_disk=0.0` (non-negative).

---

## Two Event Sources: vLLM vs Pool — Complete Logic

### vLLM Engine Events (Native msgspec Format)

**Source:** vLLM/SGLang inference engine processes.
**Transport:** ZMQ PUB or HTTP `POST /events`.
**Format:** msgspec `array_like=True` + `tag=True` + `omit_defaults=True`.

**Key difference from pool events:** vLLM events carry `token_ids` — the actual token values. This allows the conductor to **recompute** `LocalBlockHash` via XXH3, enabling proper radix tree insertion.

**Parsed event structure (`VllmEventMap`):**

``` json
["BlockStored", block_hashes, parent_hash?, token_ids, block_size, lora_id?, medium?, lora_name?, extra_keys?, group_idx?, kv_cache_spec_kind?, kv_cache_spec_sliding_window?]
```

Fields marked `?` are omitted when null (`omit_defaults=True`). The Rust deserializer uses `rmpv::Value` + tag-based dispatch + type-pattern parsing — robust against position shifts.

**Attention-group filtering:** Following Dynamo kv-router, only `FullAttention`, `MlaAttention`, and `SinkFullAttention` events are processed. SWA, Mamba, ChunkedLocal, etc. are filtered out. This ensures all ingested events share the same `block_size`, avoiding multi-group hash granularity mismatch.

**`apply_vllm_event()` logic:**

1. Parse the tagged-union array into `VllmEvent` enum (`BlockStored` / `BlockRemoved` / `AllBlocksCleared`)
2. Filter: skip non-main attention groups
3. Determine `StorageMedium` from `medium` field (default `Xpu`)
4. **HBM (Xpu) events:**
   - Compute `tokens_hash` from `token_ids` via `compute_block_hash_for_seq(block_size)`
   - Insert into `hbm_tree` via `apply_store(worker, lookup, store_data)`
   - Update reverse lookup: `seq_hash → tree_node`
5. **Non-HBM (CPU/Disk) events:**
   - Compute `tokens_hash` and cache `(block_hash, tokens_hash)` in `offload_pool_state.offload`
   - Check `pending_pool` for matching pool events → if found, insert into flat store under pool worker key
   - If not found, keep in `offload` waiting for pool confirmation

### Pool Backend Events (Mooncake/Memcache/YuanRong)

**Source:** Pool daemon (Mooncake Master, Memcache, or YuanRong).
**Transport:** ZMQ PUB only.
**Format:** msgpack map with `seq_hashes`/`block_hashes`.

**Key difference from vLLM events:** Pool events do **NOT** carry `token_ids`. They only have `seq_hashes`/`block_hashes`. The conductor cannot recompute `tokens_hash` — it must match against previously-cached offload events.

**Parsed event structure (`PoolEvent`):**

```rust
struct PoolEvent {
    event_id: u64,
    event_type: Option<String>,  // "stored" / "removed" / "cleared"
    backend_id: Option<String>,   // node IP for Mooncake/Memcache
    medium: Option<String>,       // "cpu" / "disk"
    seq_hashes: Option<Vec<FlexHash>>,
    block_hashes: Option<Vec<FlexHash>>,
    // ...
}
```

**`apply_pool_event()` logic:**

1. Parse event_type → stored / removed / cleared
2. Collect and deduplicate `seq_hashes` from both `seq_hashes` and `block_hashes` fields
3. Resolve target workers via `MatchMode`:
   - **IpOnly** (Mooncake/Memcache): lookup all DPs on the node IP → fan out to all
   - **None** (YuanRong): use subscriber's fixed `backend_id` (port = DP)
4. For each `(worker, seq_hash)`:
   - Check `offload_pool_state.offload` for `block_hash → tokens_hash`
   - If found → insert into flat store (CPU or Disk) under pool worker key
   - If not found → queue in `offload_pool_state.pending_pool` waiting for offload event

### Bidirectional Two-Phase Matching

Because pool and engine offload events arrive from **different ZMQ subscribers** and either may arrive first, the conductor uses a bidirectional cache:

``` text
         offload arrives FIRST                    pool event arrives FIRST
        ┌──────────────────────┐                ┌─────────────────────────┐
        │ ingest_offload_blocks│                │  ingest_pool_blocks     │
        │   → cache in offload │                │    → queue in           │
        │     (wait for pool)  │                │      pending_pool       │
        └──────────┬───────────┘                │      (wait for offload) │
                   │                             └───────────┬─────────────┘
                   ▼                                         ▼
        ┌──────────────────────┐                ┌─────────────────────────┐
        │ pool event arrives   │                │  offload event arrives  │
        │ → ingest_pool_blocks │                │  → ingest_offload_blocks│
        │   match in offload   │                │    match in pending_pool│
        └──────────┬───────────┘                └───────────┬─────────────┘
                   │                                         │
                   └──────────────┬──────────────────────────┘
                                  ▼
                   ┌──────────────────────────┐
                   │  Insert into radix tree  │
                   │  / CPU-Disk flat store   │
                   │  under pool worker key   │
                   └──────────────────────────┘

  Invariant: a block_hash exists in AT MOST ONE of the two maps.
  Once both sides arrive, it is removed from both and enters the tree.
```

**OffloadPoolState structure:**

```rust
pub struct OffloadPoolState {
    /// block_hash → tokens_hash (offload waiting for pool)
    pub offload: FxHashMap<u64, u64>,
    /// block_hash → workers (pool waiting for offload)
    pub pending_pool: FxHashMap<u64, FxHashSet<PendingPoolEvent>>,
}
```

**Stale entry cleanup:** `sweep_stale_pending()` runs every 100 ingest operations, evicting entries older than 60s TTL. Stale entries are also cleared on removal/cleared events.

---

## Storage Backend Factory

Three pool backends supported, each with different event broadcast semantics:

| Backend | Pool Model | Registration | MatchMode | HBM IP Index |
|------|------|-----------|-------------|
| Mooncake | Centralized master, one ZMQ PUB | `endpoint` (pool) + `medium_endpoints` (HBM) | `IpOnly` — `backend_id`=IP → all DPs on node | Yes |
| Memcache | Centralized master, one ZMQ PUB | Same as Mooncake | `IpOnly` — same as Mooncake | Yes |
| YuanRong | Per-node multi-port ZMQ PUB | `medium_endpoints` only (multi-port) | `None` — port = DP | No |

### Mooncake/Memcache (`IpOnly`)

Pool events from the central master carry `backend_id` (node IP). The conductor resolves this IP against `hbm_ip_index` — a map of `node_ip → [(instance_id, dp_rank)]` built during HBM registration. The event hash is recorded for **every** HBM-registered DP on that node. KV events do not carry an exact `dp_rank` — this avoids per-DP event routing overhead.

### YuanRong (`None`)

Each node has independent ZMQ PUB ports per storage medium. HBM, CPU and Disk events arrive on separate ports tied to specific DPs. The subscriber's `backend_id` (engine instance_id) is used for `WorkerKey` construction instead of the event's `backend_id`. Deduplication: when `cpu` and `disk` point to the same port, only one ZMQ SUB connection is created.

---

## ZMQ Event Ingestion (3-Format Dispatch)

Events arrive via ZMQ PUB as 3-part messages: `[topic] [seq: u64 BE] [msgpack payload]`.

The payload is dispatched in 3 formats, tried in order by `process_payload()`:

| # | Format | Structure | Source |
|---|------|------|
| 1 | vLLM batch | `[ts, [events...], dp_rank]` — `parse_vllm_batch()` tries both `[ts, events, dp_rank]` and `[ts, dp_rank, events]` field orders (msgspec/version robustness); ts may be `f64` or int | vLLM engine (preferred) |
| 2 | Pool batch | `(i64, Vec<PoolEvent>, u32)` via `rmp_serde` | Mooncake master |
| 3 | Memcache batch | `{"events": [PoolEvent, ...]}` via `rmp_serde` (`MemcacheEventBatch`); per-event map carries `backend_id` (node Pod IP), optional fields as nil, `seq_hashes` uint64 array (`hash_as_int=true`) or hex strings | memcache MetaService (memcache PR #334) |

The former vLLM bare / Pool legacy array (`ZmqEventMap`) / Pool bare formats have been removed — the `ZmqEventMap` type no longer exists.

**Reconnection:** `subscriber_loop_with_reconnect()` uses exponential backoff (100ms → 30s max). On disconnect, the subscriber reconnects and resumes ingestion.

**FlexHash:** Polymorphic u64 deserializer for vLLM's `ExternalBlockHash = bytes | int`:

- msgpack uint → `u64`
- msgpack binary ≤8 bytes → `u64` (big-endian); **>8 bytes (vLLM default 32-byte sha256) → trailing 8 bytes = low 64 bits**, matching vLLM int mode (`& (1 << 64) - 1`) and memcache `BlockHashHexToU64` — this is what makes engine offload events (sha256 bytes) match pool confirmations (u64)
- msgpack string (hex `0xABCD` or decimal) → parsed `u64`

Truncation does exist, but only in the separate `rmpv::Value::Binary` path in `events/vllm.rs` (used when parsing vLLM event arrays field-by-field): there, >8-byte binaries are truncated to their last 8 bytes instead of erroring.

### Replay on Registration

When a `/register` payload includes a `replay_endpoint` (and the instance is not already registered), the conductor starts a **ZMQ DEALER → ROUTER replay** session with the engine:

1. DEALER sends `[b"", start_seq: u64 BE]` (start_seq = 0)
2. ROUTER replies `[b"", seq: u64 BE, msgpack_payload]` per buffered batch
3. End-of-stream: `seq == u64::MAX` (0xFFFFFFFFFFFFFFFF)

Replay runs synchronously in a `spawn_blocking` task (blocking ZMQ I/O off the tokio runtime); each replayed batch is dispatched through the same vLLM-first, then Pool format order as the live subscriber. While `replay_in_progress > 0`, **queries are rejected** to avoid returning incomplete results during the prefix-tree rebuild. If the same worker re-registers later (`instance_exists`), replay is skipped — the tree already holds its data.

### Re-registration

Re-registering an existing `(instance_id, dp_rank)` stops the old ZMQ subscribers and:

- If the backend type changed → drops the radix-tree / flat-store data for the old registration (`remove_worker_all_media`) and clears its HBM IP index entries
- If the backend is unchanged → preserves tree data and only updates endpoint info

This lets clients fix misconfigured endpoints by simply re-registering, without a restart or explicit unregister.

---

## Query Flow (Step by Step)

``` text
POST /query {model, block_size, token_ids, tenant_id}
  │
  ▼
WorkerRegistry.query()
  │
  ▼
Indexer.query(model, tenant, token_ids, block_size)
  │
  ├─ compute_block_hash_for_seq(token_ids, block_size)
  │   → [LocalBlockHash(0xA), LocalBlockHash(0xB), ...]
  │   (XXH3, seed 1337; rayon parallel for >2048 hashes)
  │
  ├─ hbm_tree.find_matches(hashes)
  │   Traverse root → children[0xA] → children[0xB] → ...
  │   At each level: intersect active worker set with child's workers.
  │   Workers that drop out get their match depth recorded.
  │   → {WorkerKey(w1): depth=3, WorkerKey(w2): depth=1}
  │
  ├─ CPU flat lookup (sequential or parallel based on FLAT_PAR_THRESHOLD)
  │   For each hash: if cpu_blocks[hash] → add score per worker
  │
  ├─ Disk flat lookup (same pattern)
  │
  └─ Score aggregation (build_response):
      per-DP exclusive *_blocks (NPU > CPU > Disk)
      matched_tokens = (npu + cpu + disk) × block_size
      (no server-side weighting — Coordinator applies kv_affinity)
      Group by: tenant → instance → DP
```

**Response shape:**

```json
{
  "default": {
    "prefill-0": {
      "longest_matched": 384,
      "DP": {"0": {
        "matched_tokens": 384,
        "npu_blocks": 3,
        "cpu_blocks": 0,
        "disk_blocks": 0
      }}
    }
  }
}
```

Example assumes exclusive `npu_blocks=3`, `block_size=128` → coverage `matched_tokens=384`.
Coordinator affinity re-weights `*_blocks` via `scheduler_config.kv_affinity`.
Each `DpBlocks` object carries `matched_tokens` (cached prefix length in
tokens) and exclusive `npu_blocks` / `cpu_blocks` / `disk_blocks` raw counts.

---

## MessagePack Query Codec

`/query` and `/query_by_hash` negotiate the wire encoding via the request
`Content-Type` header:

- `application/msgpack` / `application/x-msgpack` → MessagePack
  (request decoded with `rmp_serde` straight into `QueryRequest` /
  `QueryByHashRequest`; response + error/empty bodies hand-encoded with
  `rmp::encode`)
- anything else (default) → JSON (historical behavior, unchanged)

**Why hand-encode the response?** `QueryResponse` uses `#[serde(flatten)]`
(`tenants` spread into the top-level map), which MessagePack serializers do
not support — the hand-written encoder guarantees the msgpack wire shape is
byte-for-byte equivalent to the JSON shape. This equivalence is guarded by
unit tests (`rmpv` → `serde_json` conversion comparison) and integration
tests (msgpack request vs JSON request on the same seeded indexer).

Key functions in `src/protocols.rs`:

- `is_msgpack_content_type(&HeaderMap) -> bool` — Content-Type sniffing
  (case-insensitive, strips `; charset=...` parameters)
- `encode_query_response_msgpack(&QueryResponse, &mut Vec<u8>)` — nested-map
  encoder mirroring the JSON shape
- `encode_error_msgpack(&str, &mut Vec<u8>)` / `encode_empty_tenant_msgpack`
  / `encode_status_ok_msgpack` — small single-map helpers

`QueryRequest` / `QueryByHashRequest` gained `Serialize` (they were
`Deserialize`-only) so `rmp_serde::to_vec` works.

---

## Motor Integration

KV Conductor is a drop-in replacement for Mooncake conductor. The Python client:

- `ConductorApiClient` in `motor/coordinator/api_client/conductor_api_client.py`
- Operations: `register_kv_instance()`, `unregister_kv_instance()`, `query_conductor()`

Deployer integration:

- K8s template: `examples/deployer/yaml_template/kv_conductor_template.yaml`
- Generator: `examples/deployer/lib/generator/kv_conductor.py`
- Startup script: `examples/deployer/startup/roles/kv_conductor.sh`

---

## Testing

Run the full test suite from the crate root (`motor/kv_conductor/`):

```bash
cd motor/kv_conductor
cargo test          # 120 unit tests in src/ + 20 integration tests
cargo clippy -- -D warnings   # enforced by pre-commit
cargo fmt --all               # enforced by pre-commit
```

### Unit Tests (`src/**/tests.rs`)

Inline test modules co-located with their code:

- `src/events/tests.rs` — event parsing (vLLM + pool formats, FlexHash, apply logic)
- `src/concurrent_tree.rs` — radix tree find_matches/apply_store/remove_worker
- `src/backend.rs` — StoreBackend/MatchMode resolution

### Integration Tests (`tests/integration_test.rs`)

HTTP API tests (20) over a real axum server on a random local port (`start_test_server()` helper binds `127.0.0.1:0`), exercising `/register`, `/unregister`, `/query`, `/query_by_hash` (msgpack), `/events`, `/health`, `/workers`. Note: `test_query_after_kv_events`'s event injection is a silent 422 in the original test (`_resp` is not asserted) — `register_and_seed()` in the msgpack tests fixes this by carrying `instance_id`; treat that helper as the canonical injection pattern. The msgpack tests assert Content-Type negotiation (`application/msgpack` request → msgpack response, errors included) and structural equality between msgpack and JSON query responses.

### Performance Profiling

Three phases in the query hot path:

| Phase | Keyword | Optimization |
|------|------|
| XXH3 hashing | `hash_computed` | rayon parallel for >2048 hashes (PAR_THRESHOLD) |
| Tree traversal | `find_matches` | Read-only locks, multiple concurrent readers |
| Total | `query profile` | `total_us = hash_us + find_matches + serialize` |

---

## Development Guide

### Adding a New Storage Backend

1. Add variant to `StoreBackend` enum in `backend.rs`
2. Implement `index_hbm_ip()`, `is_pool_auto_attach()`, `match_mode()`
3. If needed, add new `MatchMode` variant (e.g., `IpAndDpRank`)
4. Add backend-specific registration logic in `registry.rs`
5. Add tests in `backend.rs` tests module

### Adding a New Event Field

1. Add field to Python struct in `vllm/distributed/kv_events.py` (engine side)
2. Add field to `VllmEventMap` struct in `events/vllm.rs`
3. Update `parse_block_stored_values()` / `parse_block_removed_values()` position logic
4. If field affects event logic, update `apply_vllm_event()` / `apply_pool_event()`
5. Add parsing tests in `events/tests.rs`

### Adding a New Event Source Format (beyond the 2 existing)

1. Add parsing function in `zmq_subscriber.rs` (`process_payload()` dispatch)
2. Add normalization logic in `events/mod.rs` or a new sub-module
3. Wire into `IndexerEntry` application methods

### Common Debugging Workflows

**Msgpack parse errors:**

```bash
RUST_LOG=trace cargo run
# Check trace log b0/b1 hex tags: 93=fixarray(3), 9a=fixarray(10), cb=float64
# Decode hex preview against expected field order
```

**ZMQ connection issues:**

```bash
RUST_LOG=debug cargo run --features zmq
# Watch for: "ZMQ subscriber starting", "received message", reconnect backoff
```

**Query correctness:**

```bash
# Register a worker, inject known tokens, query
curl -X POST localhost:13333/query -d '{"model":"test","block_size":128,"token_ids":[1,2,3],"tenant_id":"default"}'
```

### Build & Test

```bash
cd kv-conductor
cargo build --release
cargo test --lib            # Unit tests
cargo test                  # All tests including integration
cargo run                   # HTTP only (no ZMQ)
cargo run --features zmq    # HTTP + ZMQ SUB
```
