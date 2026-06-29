# KV Conductor

Rust-based KV cache indexer service for MindIE-PyMotor. Maintains per-(model, tenant)
radix prefix trees to answer KV cache overlap queries, replacing the Mooncake conductor.

## References

- **KV Event & Interaction Protocol**: The KV cache event wire format, multi-tier storage
  model (XPU/CPU/DISK), `WorkerKey` identity (`instance_id` + `backend_id` + `dp_rank` +
  `medium`), and query response shape follow Mooncake RFC #1527:
  <https://github.com/kvcache-ai/Mooncake/issues/1527>

- **Radix Tree Design**: The prefix-tree indexing approach — concurrent radix tree with
  `Arc<RwLock<>>` nodes, per-worker reverse-lookup tables, hand-over-hand write locking,
  and `compute_block_hash_for_seq` (XXH3, seed 1337) — is inspired by the NVIDIA Dynamo
  kv-router: <https://github.com/ai-dynamo/dynamo/tree/main/lib/kv-router>

## Architecture

### Overview

KV Conductor is a standalone Rust HTTP service that maintains **radix (prefix) trees** of
cached KV blocks, indexed per `(model_name, tenant_id)` pair. It answers **KV cache overlap
queries** from routers/schedulers, enabling cache-aware request routing — i.e. steering
requests toward the worker that already has the longest matching token prefix cached.

The service replaces the Mooncake conductor for MindIE-PyMotor, with a design emphasis on:

- **Low-latency queries**: O(path_length) radix-tree traversal with `parking_lot::RwLock`
  read locks — multiple concurrent queries do not block each other.

- **Per-tenant isolation**: Each `(model, tenant)` pair gets its own radix tree, so
  multi-tenant deployments are naturally isolated.

- **Multi-tier storage awareness**: RFC #1527 storage tiers (XPU/CPU/DISK) are tracked
  independently per block, enabling the router to prefer GPU-resident cache over
  slower storage tiers.

- **Push-based ingestion**: KV cache events flow in from inference engines (vLLM/SGLang)
  and Mooncake master via HTTP `POST /events` or ZMQ SUB (opt-in via `zmq` feature).

```text
                          ┌───────────────────────────────────┐
                          │        KV Conductor Service       │
                          │         (axum 0.7 / tokio)        │
                          │                                   │
  ┌──────────┐  register  │  ┌───────────────┐                │
  │  Engine  │────────────┼─►│WorkerRegistry │                │
  │ (vLLM/   │◄───────────┼──│               │                │
  │  SGLang) │  query     │  │ instances:    │   ┌──────────┐ │
  └──────────┘            │  │  RwLock<      │   │ Indexer  │ │
                          │  │   HashMap<    │──►│          │ │
  ┌──────────┐  events    │  │    id→Entry>  │   │ DashMap< │ │
  │ Mooncake │────────────┼─►│               │   │  (model, │ │
  │  master  │  (HTTP or  │  └───────────────┘   │  tenant) │ │
  │  (ZMQ)   │   ZMQ SUB) │                      │   →Entry │ │
  └──────────┘            │                      └────┬─────┘ │
                          │                           │       │
  ┌──────────┐  GET       │        ┌──────────────────┘       │
  │  Router/ │ /health,   │        ▼                          │
  │ Scheduler│ /workers   │  ┌──────────────────┐             │
  └──────────┘            │  │ ConcurrentRadix  │             │
                          │  │      Tree        │             │
                          │  │  (per entry)     │             │
                          │  │                  │             │
                          │  │  Arc<RwLock<     │             │
                          │  │   Block>> nodes  │             │
                          │  └──────────────────┘             │
                          └───────────────────────────────────┘

```

### Component Breakdown

#### 1. HTTP Server (`server.rs`)

Axum 0.7 based HTTP server providing six endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/register` | POST | Register a worker instance with its endpoint, model, DP rank |
| `/unregister` | POST | Remove a worker; cleans up its radix-tree blocks |
| `/query` | POST | Query KV cache overlap scores for a token sequence |
| `/query_by_hash` | POST | Query KV cache overlap using pre-computed block hashes |
| `/events` | POST | Ingest KV cache events (store/remove/clear) |
| `/health` | GET | Liveness check, returns `"OK"` |
| `/workers` | GET | Debug endpoint listing all registered workers + indexer summary |

Middleware stack: CORS (permissive), HTTP tracing (`TraceLayer`).

#### 2. Worker Registry (`registry.rs`)

The central coordination point. Maintains:

- `instances: RwLock<HashMap<InstanceId, WorkerEntry>>` — registration metadata per
  instance, including endpoint info per DP rank.

- `indexer: Arc<Indexer>` — shared reference to the radix-tree indexer.

- `zmq_subscribers` (feature-gated) — active ZMQ SUB connections keyed by
  `(instance_id, dp_rank, endpoint_url)`.

- `hbm_ip_index` (feature-gated) — IP → `Vec<(instance_id, dp_rank)>` lookup table
  for pool-backend auto-attach (Mooncake, Memcache).

Key responsibilities:

- **Register**: Creates an `IndexerEntry`, records endpoint info, spawns ZMQ
  subscribers. Behaviour varies by `store_backend` (see § Storage Backends).

- **Unregister**: Stops all subscribers for the DP, cleans up radix-tree state,
  removes entries from `hbm_ip_index`.

- **Query**: Delegates to `Indexer::query()`, grouping results by instance + DP rank.

- **Apply Events**: Routes events to the correct `IndexerEntry`.

#### 2.5 Storage Backend Factory (`backend.rs`)

Encapsulates backend-specific registration and event-processing behaviour via
`StoreBackend` enum + `MatchMode` strategy:

| Backend | Pool Model | MatchMode | HBM IP Indexing |
|---------|-----------|-----------|----------------|
| Mooncake | Centralized master, one ZMQ PUB | `IpOnly` — `backend_id`=IP → all DPs on node | Yes |
| Memcache | Centralized master, one ZMQ PUB | `IpOnly` — `backend_id`=IP → all DPs on node (same as Mooncake) | Yes |
| YuanRong | Per-node multi-port ZMQ PUB | `None` — port = DP | No |

**Pool registration** (Mooncake/Memcache): uses legacy `endpoint` field, spawns a
subscriber with `MatchMode::IpOnly` auto-attach. Events from the central master carry
`backend_id` (node IP); every HBM-registered DP on that node records the event hash.

**HBM registration** (Mooncake/Memcache): uses `medium_endpoints` with `"xpu"` only.
Extracts IP from the XPU endpoint, indexes it in `hbm_ip_index`.

**Multi-port registration** (YuanRong): uses `medium_endpoints` with per-medium URLs.
Deduplicates shared ports (e.g. `cpu` + `disk` on same endpoint).

#### 3. Indexer (`indexer.rs`)

Manages a `DashMap<IndexerKey, Arc<IndexerEntry>>` — a concurrent hash map keyed by
`(model_name, tenant_id)`. Stores a `ScoringConfig` for per-medium block weights
(configurable at startup via `--hbm-weight/--cpu-weight/--disk-weight`).

Each `IndexerEntry` holds per-medium data structures:

| Medium | Structure | Weight | Rationale |
|--------|-----------|--------|-----------|
| HBM/XPU | `hbm_tree: ConcurrentRadixTree` (prefix chain) | ×3 | Prefix-tree enables O(L) contiguous-prefix queries |
| CPU | `cpu_blocks: FxHashMap<tokens_hash, FxHashSet<WorkerKey>>` (flat) | ×2 | Pool blocks are isolated — no chain relationship |
| Disk | `disk_blocks` (same flat HashMap) | ×1 | Same as CPU — "is this block cached?" lookup |
| HBM reverse | `lookups: FxHashMap<WorkerKey, WorkerLookup>` | — | seq_hash → tree node for O(1) removal |
| CPU/Disk reverse | `cpu_lookups/disk_lookups` | — | seq_hash → tokens_hash for O(1) flat removal |
| Offload cache | `non_hbm_cache: FxHashMap<u64, u64>` | — | engine block_hash → tokens_hash for two-phase pool confirm |

The `Indexer::query()` method:

1. Computes `LocalBlockHash` values from the token sequence via `compute_block_hash_for_seq()`.
2. HBM: prefix-tree traversal → per-worker depth (blocks) × hbm_weight.
3. CPU: flat lookup → each matched block × cpu_weight.
4. Disk: flat lookup → each matched block × disk_weight.
5. DP total score = sum of all three media scores.
6. Groups results: per-tier scores + per-DP breakdown, per instance, per tenant.

#### 4. Radix Tree (`radix_tree.rs`, `concurrent_tree.rs`)

Two implementations exist:

- **`RadixTree`** (`radix_tree.rs`): Single-threaded reference implementation using
  `Rc<RefCell<>>`. Primarily used for unit testing correctness.

- **`ConcurrentRadixTree`** (`concurrent_tree.rs`): **Production implementation** using
  `Arc<parking_lot::RwLock<Block>>` per node. This is the tree type used by `IndexerEntry`.

**Tree Structure:**

```text
  root (no block_hash)
   │
   ├─[LocalBlockHash(0xA)]── Block { workers: {W1:0, W2:0}, children: ... }
   │    │
   │    ├─[LocalBlockHash(0xB)]── Block { workers: {W1:0}, children: ... }
   │    │    └─[LocalBlockHash(0xC)]── Block { workers: {W1:0} }
   │    │
   │    └─[LocalBlockHash(0xD)]── Block { workers: {W2:0} }
   │
   └─[LocalBlockHash(0xE)]── Block { workers: {W3:0} }

```

Each `Block` node contains:

- `children: FxHashMap<LocalBlockHash, SharedBlock>` — child blocks keyed by
  **token-content hash** (XXH3 of token bytes in the block).

- `workers: FxHashSet<WorkerKey>` — which workers have this block cached.

- `block_hash: Option<SequenceBlockHash>` — the engine-provided rolling sequence hash

  (for reverse lookup).

**Concurrency Model:**

- **`find_matches()`** (query path): Acquires only read locks, traversing from root
  along the query sequence. At each level, intersects the active worker set with the
  child's workers. Workers that drop out get their match depth recorded. Multiple
  concurrent queries proceed simultaneously.

- **`apply_store()` / `apply_remove()`** (mutation path): The caller holds an exclusive
  write lock on the worker's `WorkerLookup` table. Tree mutations use hand-over-hand
  write locking (parent locked, then child locked, then parent released). The external
  lookup table provides O(1) access to any cached block by its sequence hash.

**Memory Reclamation:** When the last worker is removed from a block, the block's
children are cleared, allowing the subtree to be dropped when no longer referenced.

#### 5. Hashing (`hashing.rs`)

Computes `LocalBlockHash` values from token sequences using **XXH3** (seed `1337`,
consistent with Dynamo kv-router):

```text
tokens: [1, 2, 3, 4, 5, 6, 7, 8]  (block_size=4)
         └──┬──┘  └──┬──┘
         XXH3(1,2,3,4)  XXH3(5,6,7,8)
              = 0xA         = 0xB

query: compute_block_hash_for_seq(tokens, block_size) → [LocalBlockHash(0xA), LocalBlockHash(0xB)]
tree:  root → children[0xA] → children[0xB] → match!

```

`block_size` is passed by the caller (from `QueryRequest.block_size`), not stored
in the indexer. Hashes computed at different `block_size` values coexist in the
same tree — they are distinct u64 values with no collision risk. The Coordinator
must use the same `block_size` that the engine uses for KV event publishing.
For DeepSeek V4, the main attention group (Full MLA) uses a unified `block_size`
(e.g. 256); events from SWA and other non-main attention groups are filtered
out by the conductor, matching Dynamo kv-router's strategy.

#### 5.1 Non-HBM Event Caching (Two-Phase Matching)

The engine may offload KV blocks to CPU/DISK via the pool backend (Mooncake
Master). The engine publishes a store event *before* the pool backend places
the block; the pool backend later confirms placement with its own event.
Because the pool backend may store the block on a different node than the
engine that offloaded it, the conductor uses a **two-phase cache strategy**:

```text
Phase 1 — Engine offloading event (medium=cpu/disk):
  block_hash (SHA256) → cache[tokens_hash] = XXH3(token_ids)
  NOT inserted into radix tree.

Phase 2 — Pool backend confirms placement (store event from Mooncake Master):
  Look up block_hash in cache → get tokens_hash (XXH3)
  Insert into radix tree under the pool backend's worker node.

Phase 3 — Pool backend eviction (remove event):
  Look up block_hash in cache → find tokens_hash → remove from tree
  → evict cache entry.
```

This ensures the radix tree always maps to the *actual* node where the
KV block resides, not the node that originally offloaded it.

#### 6. ZMQ Subscriber (`zmq_subscriber.rs`, feature-gated)

Opt-in feature (`--features zmq`) for push-based event ingestion from Mooncake master.
When a worker registers with `"type": "Mooncake"`, a `ZmqSubscriber` is spawned:

- Connects to the worker's endpoint as a ZMQ SUB socket.
- Receives multi-part msgpack messages: `[topic] [sequence_number] [payload]`.

- Payload format: `(timestamp_ms, [event_maps...], dp_rank)`.

- Parses events, normalizes them (supports both RFC #1527 and legacy formats), and
  applies them to the correct `IndexerEntry`.

- Runs on a `spawn_blocking` task (ZMQ is synchronous), with a `CancellationToken`
  for graceful shutdown on unregister.

#### 7. Protocol Types (`protocols.rs`)

Defines the full HTTP API contract, compatible with the Python `ConductorApiClient`
in `motor/coordinator/api_client/`:

**Hash Types:**

- `LocalBlockHash(u64)`: XXH3-based token-content hash — primary radix-tree key.
- `SequenceBlockHash(u64)`: Engine-provided rolling hash (includes parent context) —
  used in per-worker reverse-lookup tables.

**Identity:**

- `WorkerKey { instance_id, backend_id, dp_rank, medium }`: Composite key used in
  radix-tree worker sets, enabling per-DP-rank and per-storage-tier differentiation.
  `backend_id` follows RFC #1527 — it may differ from `instance_id` when blocks
  originate from a Mooncake daemon rather than the engine itself.

**Storage Tiers (`StorageMedium`):**

| Enum | RFC #1527 | Source | Typical Medium |
|---|---|---|---|
| `Xpu` | XPU | Engine worker events | GPU/NPU HBM |
| `Cpu` | CPU | Mooncake MEMORY replica | Host DDR |
| `Disk` | DISK | Mooncake DISK replica | SSD/NVMe/DFS |
| `Unknown` | — | Fallback | Treated as XPU |

**Wire Format Normalization (`KvEventWirePayload`):**
Accepts both engine-style JSON (`{"type": "stored", "blocks": [...], "parent_hash": ...}`)
and RFC #1527-style (`{"event_type": "stored", "seq_hashes": [...], "medium": "cpu", ...}`),
normalizing them into the canonical `KvCacheEventData` enum used by the radix tree.

### Data Flow

#### Registration Flow

```text
Engine/Router                    KV Conductor
     │                                │
     │  POST /register                │
     │  {instance_id, dp_rank,        │
     │   modelname, block_size, ...}  │
     │───────────────────────────────►│
     │                                ├─ WorkerRegistry.register()
     │                                │   ├─ Check duplicate (instance_id, dp_rank)
     │                                │   ├─ Indexer.get_or_create(model, tenant, block_size)
     │                                │   │    └─ New IndexerEntry { ConcurrentRadixTree, lookups }
     │                                │   ├─ Insert WorkerEntry into instances map
     │                                │   └─ [if Mooncake] Spawn ZmqSubscriber
     │  201 {"status": "ok"}          │
     │◄───────────────────────────────│

```

#### Query Flow

```text
Router/Scheduler                  KV Conductor
     │                                │
     │  POST /query                   │
     │  {model, block_size,           │
     │   token_ids, tenant_id}        │
     │───────────────────────────────►│
     │                                ├─ WorkerRegistry.query()
     │                                │   └─ Indexer.query(model, tenant, token_ids)
     │                                │       ├─ compute_block_hash_for_seq(tokens, block_size)
     │                                │       │    → [LocalBlockHash(0xA), LocalBlockHash(0xB), ...]
     │                                │       ├─ ConcurrentRadixTree.find_matches(hashes)
     │                                │       │    └─ Traverse root→children[0xA]→children[0xB]→...
     │                                │       │       Intersect worker sets per level
     │                                │       │       → {WorkerKey(w1): 3 blocks, WorkerKey(w2): 1 block}
     │                                │       └─ Scale: matched_blocks × block_size → tokens
     │                                │          Group: tenant → instance → {longest_matched, XPU, CPU, DISK, DP}
     │  200 {"default": {             │
     │    "vllm-0": {                 │
     │      "longest_matched": 384,   │
     │      "XPU": 384, "CPU": 0,     │
     │      "DISK": 0,                │
     │      "DP": {"0": 384}          │
     │    }}}                         │
     │◄───────────────────────────────│

```

#### Event Ingestion Flow (HTTP)

```text
Engine (vLLM/SGLang)             KV Conductor
     │                                │
     │  POST /events                  │
     │  {events: [{event_id,          │
     │   data: {type: "stored",       │
     │     parent_hash, blocks}}]}    │
     │───────────────────────────────►│
     │                                ├─ KvEventWirePayload.normalize()
     │                                │   └─ Engine format → KvCacheEventData::Stored
     │                                ├─ IndexerEntry.apply_event(worker, event)
     │                                │   └─ ConcurrentRadixTree.apply_store(worker, lookup, store_data)
     │                                │       ├─ Lock worker's lookup (write)
     │                                │       ├─ Find/insert blocks in tree (hand-over-hand write locks)
     │                                │       ├─ Update reverse lookup: seq_hash → block
     │                                │       └─ Insert worker into terminal block's worker set
     │  200 {"status": "ok",          │
     │   "events_applied": N}         │
     │◄───────────────────────────────│

```

### ZMQ Event Wire Format

Events arrive via ZMQ PUB as 3-part messages: `[topic] [seq: u64 BE] [msgpack payload]`.

The payload is dispatched in **5 formats**, tried in order:

**Format 1 — vLLM msgspec batch** (preferred, `array_like=True` + `tag=True`):

```
[ts: f64, [["BlockStored", block_hashes, token_ids, block_size, 0, "GPU", 0, "FullAttention"]], dp_rank: int|null]
```

Events are tagged-union arrays with `omit_defaults=True` — null fields are absent from the array.
The Rust deserializer uses `rmpv::Value` + tag-based dispatch + type-pattern parsing.

**Format 2 — vLLM bare event** (single event, no batch wrapper):

```
["BlockRemoved", block_hashes]
```

**Format 3 — Map batch** (Mooncake Master):

```msgpack
{"timestamp_ms": 1782281033484, "dp_rank": 0, "events": [
  {"event_id": 66, "event_type": "stored", "medium": "cpu",
   "seq_hashes": [7575625822238262545]}
]}
```

**Format 4 — Array batch** (Mooncake legacy):

```msgpack
[1782281033484, [{"event_type": "stored", ...}], 0]
```

**Format 5 — Bare Mooncake event** (single event, no batch wrapper).

Events **missing** `model_name` / `block_size` / `dp_rank` / `medium` use
the subscriber's registration-time defaults.

### Error Model

| Error | HTTP Status | Trigger |
|---|---|---|
| `DuplicateRegistration` | 409 Conflict | Same (instance_id, dp_rank) registered twice |
| `InstanceNotFound` | 404 Not Found | Unregister or event for unknown instance |
| `NoIndexer` | 404 Not Found | Query before any worker registered for (model, tenant) |
| `NoWorkers` | 200 OK (empty `{}`) | Query with no cached blocks — not an error |
| `ParentBlockNotFound` | 500 | Store event references unknown parent hash |
| `InvalidBlockSequence` | 500 | Self-referencing block detected |
| `BlockNotFound` | 500 (best-effort) | Remove event for unknown block hash |

### Module Dependency Graph

```text
main.rs
  └── server.rs (axum HTTP)
        └── registry.rs
              ├── indexer.rs
              │     ├── concurrent_tree.rs
              │     │     └── protocols.rs  (WorkerKey, LocalBlockHash, SequenceBlockHash, OverlapScores)
              │     ├── hashing.rs          (compute_block_hash_for_seq)
              │     └── protocols.rs        (KvCacheEventData, QueryRequest/Response, ...)
              ├── backend.rs                (StoreBackend, MatchMode — drives register/unregister behaviour)
              ├── protocols.rs
              └── zmq_subscriber.rs [optional: zmq feature]
                    ├── backend.rs          (MatchMode — pool event auto-attach)
                    ├── indexer.rs
                    └── protocols.rs

error.rs  ←  used by all modules

```

## Quick Start

```bash
cargo build --release
./target/release/kv-conductor --port 13333

# With custom scoring weights:
./target/release/kv-conductor --port 13333 --hbm-weight 3 --cpu-weight 2 --disk-weight 1
```

## API

### POST /register

Two registration protocols are supported — the new `medium_endpoints` map for
per-medium endpoint URLs, and the legacy `endpoint` string for backward
compatibility with Mooncake Master.

**Mooncake HBM (per-DP) + Pool:**

```json
// HBM registration (one per DP)
{
  "instance_id": "prefill-0",
  "medium_endpoints": {"xpu": "tcp://10.0.0.1:50090"},
  "type": "vLLM",
  "store_backend": "Mooncake",
  "modelname": "llama-7b",
  "block_size": 128,
  "dp_rank": 0
}

// Pool registration (once per cluster — legacy endpoint field)
{
  "instance_id": "mooncake-pool",
  "endpoint": "tcp://mooncake-master:5557",
  "type": "Mooncake",
  "store_backend": "Mooncake",
  "modelname": "llama-7b",
  "block_size": 128,
  "dp_rank": 0
}
```

**YuanRong multi-port (per-DP):**

```json
{
  "instance_id": "yr-node-0",
  "medium_endpoints": {
    "xpu":  "tcp://10.0.0.1:15557",
    "cpu":  "tcp://10.0.0.1:15558",
    "disk": "tcp://10.0.0.1:15558"
  },
  "type": "vLLM",
  "store_backend": "YuanRong",
  "modelname": "llama-7b",
  "block_size": 128,
  "dp_rank": 0
}
```

**Memcache (IP-only matching, same as Mooncake):**

```json
{
  "instance_id": "mem-pf-0",
  "medium_endpoints": {"xpu": "tcp://10.0.0.1:50090"},
  "type": "vLLM",
  "store_backend": "Memcache",
  "modelname": "llama-7b",
  "block_size": 128,
  "dp_rank": 0
}
```

Pool registration for Memcache uses the same legacy `endpoint` field as Mooncake.
Both Mooncake and Memcache use `IpOnly` matching: events carry `backend_id` (node IP),
and every HBM-registered DP on that node records the event hash — KV events do not
carry an exact `dp_rank`.

### POST /query

```json
{
  "model": "llama-7b",
  "block_size": 128,
  "token_ids": [101, 102, 456],
  "tenant_id": "default"
}

```

Response:

```json
{
  "default": {
    "vllm-prefill-42": {
      "longest_matched": 384,
      "XPU": 384,
      "CPU": 0,
      "DISK": 0,
      "total_score": 1152,
      "DP": {
        "0": { "XPU": 384, "CPU": 0, "DISK": 0, "total": 384 }
      }
    }
  }
}
```

Each DP's score is the sum of per-medium weighted scores: `total = XPU + CPU + DISK`.
Weights are configurable at startup.

### POST /query_by_hash

Query using pre-computed `LocalBlockHash` values instead of raw token IDs.
This avoids redundant XXH3 hashing when the caller has already computed hashes.

```json
{
  "model": "llama-7b",
  "block_size": 128,
  "block_hashes": [12345678901234567890, 9876543210987654321],
  "tenant_id": "default"
}

```

Response shape is identical to `POST /query`.

### POST /events

Ingest KV cache events (store, remove, clear) from a worker instance.
The instance must either be pre-registered via `/register`, or the batch must
include `model_name` and `tenant_id` for auto-provisioning.

```json
{
  "instance_id": "vllm-prefill-42",
  "model_name": "llama-7b",
  "tenant_id": "default",
  "block_size": 128,
  "events": [
    {
      "event_id": 1,
      "data": {
        "type": "stored",
        "parent_hash": null,
        "blocks": [
          {"block_hash": 100, "tokens_hash": 12345678901234567890}
        ]
      },
      "dp_rank": 0
    }
  ],
  "shutdown": false
}

```

Response:

```json
{"status": "ok", "events_applied": 1}

```

### GET /workers

Returns a list of all registered workers with their model, tenant, block_size,
and per-DP-rank endpoint info. Supports optional `?model_name=` and `?tenant_id=`
query parameters for filtering.

## Docker

```bash
docker build -t kv-conductor -f Dockerfile ..
docker run -p 13333:13333 kv-conductor

```

## Motor Integration

KV Conductor is a drop-in replacement for the Mooncake conductor in MindIE-PyMotor.
The `ConductorApiClient` in [`motor/coordinator/api_client/conductor_api_client.py`](../motor/coordinator/api_client/conductor_api_client.py)
already speaks the same wire protocol:

| Client Operation | HTTP Call | kv-conductor Handler |
|---|---|---|
| `register_kv_instance()` | `POST /register` | Fully compatible — `instance_id`, `endpoint`, `type`, `modelname`, `block_size`, `dp_rank`, `tenant_id`, `replay_endpoint` |
| `unregister_kv_instance()` | `POST /unregister` | Fully compatible — same fields minus `endpoint`/`replay_endpoint` |
| `query_conductor()` | `POST /query` | Fully compatible — `model`, `block_size`, `token_ids`, `tenant_id`; response shape matches `rsp[tenant][instance]["longest_matched"]` / `["DP"][rank]` |

### Deployer (One-Click Launch)

The deployer under [`examples/deployer/`](../examples/deployer/) already has kv-conductor
scaffolding:

- **K8s template**: [`yaml_template/kv_conductor_template.yaml`](../examples/deployer/yaml_template/kv_conductor_template.yaml) —
  Deployment + ClusterIP Service on port 13333, with `KV_CONDUCTOR_SERVICE` and `RUST_LOG`
  environment variables.

- **Generator**: [`lib/generator/kv_conductor.py`](../examples/deployer/lib/generator/kv_conductor.py) —
  fills the template with the Motor image, namespace, port, and injects service discovery
  env vars.

- **Startup script**: [`startup/roles/kv_conductor.sh`](../examples/deployer/startup/roles/kv_conductor.sh) —
  locates the `kv-conductor` binary under `/usr/local/bin/` or `/opt/motor/bin/`, reads
  `KV_CONDUCTOR_PORT` / `KV_CONDUCTOR_HOST` from the environment, and starts the service
  as the container's main process.

- **boot.sh** dispatches to the script when the pod's `ROLE` env var is `kv_conductor`.

#### Prerequisites

1. The `kv-conductor` binary must be baked into the Motor Docker image at one of the
   searched paths (`/usr/local/bin/kv-conductor` or `/opt/motor/bin/kv-conductor`).
2. The coordinator's user config must point to the kv-conductor service:

```json
{
  "motor_coordinator_config": {
    "prefill_kv_event_config": {
      "conductor_service": "kv-conductor",
      "http_server_port": 13333,
      "block_size": 128,
      "engine_type": "vllm"
    }
  }
}

```

#### Data Flow After Deployment

```text
  Engine Pod                          KV Conductor Pod
  (ROLE=prefill)                      (ROLE=kv_conductor)
    │                                      │
    │  [1] Register                        │
    │  POST /register ──────────────────►  WorkerRegistry.register()
    │                                       ├─ Indexer.get_or_create(model, tenant)
    │                                       └─ [Mooncake] spawn ZmqSubscriber
    │◄── 201 {"status":"ok"} ────────────  │
    │                                      │
    │  [2] KV Events (store/remove/clear)  │
    │  POST /events ────────────────────►  IndexerEntry.apply_event()
    │  or ZMQ PUB ──────────────────────►  ConcurrentRadixTree.apply_store()
    │                                      │
    │  [3] Cache-Aware Query               │
    │  POST /query ─────────────────────►  Indexer.query()
    │       {model, token_ids, block_size}  ├─ compute_block_hash_for_seq()
    │                                       └─ tree.find_matches()
    │◄── 200 {                             │
    │       "default": {                   │
    │         "vllm-prefill-0": {          │
    │           "longest_matched": 384,    │
    │           "XPU": 384,                │
    │           "DP": {"0": 384}           │
    │         }                            │
    │       }                              │
    │     } ─────────────────────────────  │
    │                                      │
```

## Testing

```bash
cargo test
```
