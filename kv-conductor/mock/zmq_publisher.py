#!/usr/bin/env python3
"""
Mock ZMQ KV Event Publisher for kv-conductor e2e testing.

Simulates realistic engine KV cache behavior with sensible defaults:

  - Multi-medium (XPU port + CPU/DISK port) — ``--single-port`` to disable
  - vLLM msgspec wire format (BlockStored/Removed/Cleared with token_ids)
    — ``--mooncake-format`` for legacy Mooncake format
  - SWA attention events mixed in (~20% of stored events) for filtering tests
    — ``--no-swa-mixed`` to disable

Binds ZMQ PUB sockets and broadcasts KV events.  Supports an interactive
CLI for registering with kv-conductor.

Usage:
    python zmq_publisher.py --model opt-125m --dp-rank 0 --block-size 128
    python zmq_publisher.py --single-port --mooncake-format --no-swa-mixed
"""

import argparse
import os
import random
import struct
import sys
import threading
import time

try:
    import zmq
except ImportError:
    print("pyzmq not installed. Run: pip install pyzmq")
    sys.exit(1)
try:
    import msgpack
except ImportError:
    print("msgpack not installed. Run: pip install msgpack")
    sys.exit(1)
try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)

from token_pool import TOKEN_POOL


# ---------------------------------------------------------------------------
# IPv6 helpers
# ---------------------------------------------------------------------------


def _zmq_endpoint(host: str, port: int) -> str:
    """Build a ZMQ tcp:// endpoint URL, bracketing IPv6 addresses."""
    if ":" in host:
        return f"tcp://[{host}]:{port}"
    return f"tcp://{host}:{port}"


def _pod_ip(default: str = "127.0.0.1") -> str:
    return os.environ.get("POD_IP", default)


# ---------------------------------------------------------------------------
# XXH3 hashing — replicates kv-conductor's compute_block_hash_for_seq
# ---------------------------------------------------------------------------

SEED = 1337


def compute_block_hashes(tokens, block_size):
    """XXH3, seed 1337, sliding window of block_size u32 tokens, little-endian."""
    import xxhash

    hashes = []
    for i in range(0, len(tokens), block_size):
        chunk = tokens[i : i + block_size]
        buf = struct.pack(f"<{len(chunk)}I", *chunk)
        hashes.append(xxhash.xxh3_64_intdigest(buf, SEED))
    return hashes


# ---------------------------------------------------------------------------
# Realistic token generation — uses shared TOKEN_POOL
# ---------------------------------------------------------------------------


def generate_tokens_for_publisher(dp_rank, block_size, block_index):
    """Deterministic unique tokens per (dp_rank, block_index).

    Uses hash-based generation from token_pool.generate_tokens — each
    block gets a distinct sequence, no cycling, no cross-DP collisions.
    """
    from token_pool import generate_tokens

    return generate_tokens(dp_rank, block_size, block_index)


# ---------------------------------------------------------------------------
# Mock Publisher with realistic event patterns
# ---------------------------------------------------------------------------


class MockZmqPublisher:
    """Publishes KV cache events with realistic store/remove/clear mix."""

    def __init__(
        self,
        port: int,
        model_name: str,
        dp_rank: int,
        block_size: int,
        initial_blocks: int,
        interval: float,
        instance_id: str = None,
        tenant_id: str = "default",
        backend_id: str = "",
        vllm_format: bool = True,
        swa_mixed: bool = True,
        offload_queue: list = None,  # shared queue for two-phase simulation
        is_pool_publisher: bool = False,  # True for CPU/DISK pool publisher
    ):
        self.port = port
        self.model_name = model_name
        self.dp_rank = dp_rank
        self.block_size = block_size
        self.interval = interval
        self.instance_id = instance_id or f"mock-{model_name}-dp{dp_rank}"
        self.backend_id = backend_id or _pod_ip()
        self.tenant_id = tenant_id
        self.vllm_format = vllm_format
        self.swa_mixed = swa_mixed
        self.offload_queue = offload_queue  # None → offloading disabled; list → enabled
        self.is_pool_publisher = is_pool_publisher

        self._event_counter = 0

        # State
        self._publishing = True
        self._running = True
        self._registered = False
        self._conductor_url = None
        self._event_id = 0
        self._stats_stored = 0
        self._stats_removed = 0
        self._stats_cleared = 0
        self._lock = threading.Lock()

        # Deterministic RNG per publisher
        self._rng = random.Random(dp_rank * 12345 + port * 67890)  # nosec B311

        # Track last published block hash for cross-event parent chaining.
        # When set, the next vLLM BlockStored event will carry this as
        # parent_block_hash so blocks form a contiguous prefix chain.
        self._last_published_block_hash: int | None = None

        # --- Virtual KV cache ---
        # Pool publishers do not maintain their own cache — they only drain the
        # shared offload queue and emit Mooncake-format confirm events.
        self._active_blocks = {}
        if is_pool_publisher:
            self._max_blocks = 0
        else:
            self._max_blocks = max(initial_blocks, 4)
        self._growth_interval = 120
        self._growth_rate = 0.30
        self._growth_timer = time.time()
        self._next_seq = 1

        # Pre-generate initial blocks (pool publisher skips this).
        # After pre-generation, bump _max_blocks so the publish loop has room
        # to store NEW blocks (not just remove old ones). Without this, the
        # loop never calls _make_store_batch and offloading never fires.
        if not is_pool_publisher:
            for _ in range(self._max_blocks * block_size // block_size or 1):
                self._generate_and_store_block()
            self._max_blocks = self._max_blocks * 5 // 4  # +25% headroom

        # ZMQ
        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.PUB)
        self._socket.bind(f"tcp://*:{port}")
        self._total_pub = 0
        self._total_rem = 0
        fmt_name = "vLLM" if vllm_format else "Mooncake"
        swa_note = " +SWA" if swa_mixed else ""
        print(f"[mock] ZMQ PUB bound on tcp://*:{port}")
        print(
            f"[mock] model={model_name} dp={dp_rank} block_size={block_size} "
            f"format={fmt_name}{swa_note} pool={len(TOKEN_POOL)}tokens "
            f"init_capacity={self._max_blocks} interval={interval}s"
        )

    # ── Virtual cache operations ──────────────────────────────────────

    def _generate_and_store_block(self):
        tokens = generate_tokens_for_publisher(self.dp_rank, self.block_size, len(self._active_blocks))
        th = compute_block_hashes(tokens, self.block_size)[0]
        seq = self._next_seq
        self._next_seq += 1
        self._active_blocks[seq] = (th, tokens)
        return seq, th, tokens

    def _evict_block(self):
        if not self._active_blocks:
            return None
        oldest_seq = min(self._active_blocks.keys())
        th, tokens = self._active_blocks.pop(oldest_seq)
        return oldest_seq, th, tokens

    # ── Event building ────────────────────────────────────────────────

    def _build_events(self):
        # Pool publisher (CPU/DISK): drain the shared offload queue and emit
        # Mooncake-format confirm events with matching seq_hashes.
        if self.is_pool_publisher:
            return self._drain_offload_queue()

        now = time.time()
        if now - self._growth_timer >= self._growth_interval:
            old_max = self._max_blocks
            self._max_blocks = max(old_max + 1, int(old_max * (1 + self._growth_rate)))
            self._growth_timer = now
            added = self._max_blocks - old_max
            print(
                f"[growth] capacity {old_max} → {self._max_blocks} (+{added}, active={len(self._active_blocks)})",
                flush=True,
            )

        current = len(self._active_blocks)
        roll = self._rng.random()

        if current < self._max_blocks and roll < 0.80:
            return self._make_store_batch(min(16, self._max_blocks - current))
        elif current > max(4, self._max_blocks // 2) and roll < 0.95:
            return self._make_remove_batch(min(8, current - self._max_blocks // 2))
        elif roll < 0.995 and current > 0:
            return self._make_clear_event()
        else:
            return self._make_store_batch(4)

    def _drain_offload_queue(self):
        """Pool publisher: confirm offloaded blocks with Mooncake-format events.

        Reads block hashes from the shared offload queue and emits stored
        events with matching seq_hashes, simulating the pool backend confirming
        placement of engine-offloaded blocks.
        """
        if not self.offload_queue:
            return []

        with self._lock:
            self._event_id += 1
            eid = self._event_id

        # Drain up to 48 items per batch to keep up with 60% offload rate.
        count = min(48, len(self.offload_queue))
        hashes = []
        for _ in range(count):
            if self.offload_queue:
                h = self.offload_queue.pop(0)
                hashes.append(h)

        if not hashes:
            return []

        # Mooncake-format confirm event — seq_hashes match the engine's block_hashes.
        # The kv-conductor's apply_zmq_event looks up seq_hashes in the non-HBM
        # cache and inserts the corresponding XXH3 tokens_hash into the radix tree.
        # Emit two batches: one for CPU, one for DISK.
        # Split hashes roughly 60/40 to simulate different pool tiers.
        split = max(1, len(hashes) * 3 // 5)
        cpu_hashes = hashes[:split]
        disk_hashes = hashes[split:]
        batches = []
        ts = int(time.time() * 1000)

        for medium, hlist in [("cpu", cpu_hashes), ("disk", disk_hashes)]:
            if not hlist:
                continue
            with self._lock:
                self._event_id += 1
                eid = self._event_id
            event = {
                "event_id": eid,
                "event_type": "stored",
                "model_name": self.model_name,
                "tenant_id": self.tenant_id,
                "backend_id": self.instance_id,
                "medium": medium,
                "dp_rank": self.dp_rank,
                "seq_hashes": hlist,
                "block_size": self.block_size,
            }
            batches.append([ts, [event], self.dp_rank])

        print(f"[pool] confirm offload cpu={len(cpu_hashes)} disk={len(disk_hashes)} hashes={hashes[:3]}", flush=True)
        return batches

    def _make_store_batch(self, count):
        if count <= 0:
            return []

        with self._lock:
            self._event_id += 1
            eid = self._event_id

        blocks = []
        tokens_hashes = []
        all_tokens = []

        for _ in range(count):
            _, th, tokens = self._generate_and_store_block()
            blocks.append({"block_hash": th, "tokens_hash": th})
            tokens_hashes.append(th)
            all_tokens.extend(tokens)

        with self._lock:
            self._stats_stored += count
            self._total_pub += count
            self._event_counter += 1

        # SWA mixed: every 5th stored event uses SlidingWindow spec_kind
        if self.swa_mixed and self._event_counter % 5 == 0:
            spec_kind = "SlidingWindow"
        else:
            spec_kind = "FullAttention"  # vLLM main-attention spec

        # CPU offload: ~60% of batches are offloaded to simulate real
        # workloads where most KV blocks reside in CPU/disk tiers.
        # Offloaded hashes are pushed to the shared queue for the pool
        # publisher to confirm via Mooncake-format events.
        is_offload = self.offload_queue is not None and self._event_counter % 5 >= 2
        medium = "cpu" if is_offload else None

        if self.vllm_format:
            # vLLM msgspec array format (array_like=True, tag=True,
            # omit_defaults=True).  Include int anchors (lora_id=0,
            # group_idx=0) so Rust's type-pattern parser stays synced.
            # Fields: [tag, block_hashes, token_ids, block_size,
            #          lora_id, medium, group_idx, kv_cache_spec_kind]
            # (parent_block_hash, lora_name, extra_keys, sliding_window omitted)
            event = [
                "BlockStored",
                tokens_hashes,
                all_tokens,
                self.block_size,
                0,  # lora_id (int anchor)
                medium or "GPU",  # medium (str anchor)
                0,  # group_idx (int anchor)
                spec_kind,
            ]
            if is_offload:
                self.offload_queue.extend(tokens_hashes)
        else:
            event = {
                "event_id": eid,
                "event_type": "stored",
                "model_name": self.model_name,
                "tenant_id": self.tenant_id,
                "dp_rank": self.dp_rank,
                "backend_id": self.backend_id,
                "block_size": self.block_size,
                "blocks": blocks,
                "parent_hash": None,
                "seq_hashes": tokens_hashes,
            }

        self._print_publish("STORE", eid, count, tokens_hashes, spec_kind)
        ts = time.time() if self.vllm_format else int(time.time() * 1000)
        return [[ts, [event], self.dp_rank]]

    def _make_remove_batch(self, count):
        removed = []
        removed_ths = []
        for _ in range(count):
            r = self._evict_block()
            if r:
                removed.append(r[0])
                removed_ths.append(r[1])
        if not removed:
            return []

        with self._lock:
            self._event_id += 1
            eid = self._event_id
            self._stats_removed += len(removed)
            self._total_rem += len(removed)

        if self.vllm_format:
            # vLLM msgspec array: [tag, block_hashes]
            # (medium, group_idx omitted — omit_defaults=True)
            event = [
                "BlockRemoved",
                removed_ths,
            ]
        else:
            event = {
                "event_id": eid,
                "event_type": "removed",
                "model_name": self.model_name,
                "tenant_id": self.tenant_id,
                "dp_rank": self.dp_rank,
                "backend_id": self.backend_id,
                "block_size": self.block_size,
                "block_hashes": removed_ths,
            }

        self._print_publish("REMOVE", eid, len(removed_ths), removed_ths, None)
        ts = time.time() if self.vllm_format else int(time.time() * 1000)
        return [[ts, [event], self.dp_rank]]

    def _make_clear_event(self):
        old_count = len(self._active_blocks)
        self._active_blocks.clear()
        self._next_seq = 1
        self._last_published_block_hash = None  # chain broken

        with self._lock:
            self._event_id += 1
            eid = self._event_id
            self._stats_cleared += 1

        if self.vllm_format:
            # vLLM msgspec array: [tag]
            event = ["AllBlocksCleared"]
        else:
            event = {
                "event_id": eid,
                "event_type": "cleared",
                "model_name": self.model_name,
                "tenant_id": self.tenant_id,
                "dp_rank": self.dp_rank,
                "backend_id": self.backend_id,
                "block_size": self.block_size,
            }

        self._print_publish("CLEAR", eid, old_count, [], None)
        ts = time.time() if self.vllm_format else int(time.time() * 1000)
        return [[ts, [event], self.dp_rank]]

    def _print_publish(self, kind, eid, count, hashes, spec_kind=None):
        h_preview = hashes[:3] if hashes else []
        active = len(self._active_blocks)
        capacity = self._max_blocks
        kind_extra = f" kind={spec_kind}" if spec_kind else ""
        print(
            f"[publish] {kind}{kind_extra} batch=#{eid} blocks={count} "
            f"seq_hashes={h_preview} cache={active}/{capacity} "
            f"stored={self._stats_stored} removed={self._stats_removed} "
            f"cleared={self._stats_cleared}",
            flush=True,
        )

    # ── Publish loop ──────────────────────────────────────────────────

    def _publish_loop(self):
        print(f"[mock] publish loop started, initial_cache={len(self._active_blocks)}/{self._max_blocks}")
        while self._running:
            if self._publishing:
                for payload in self._build_events():
                    packed = msgpack.packb(payload)
                    self._socket.send(b"", zmq.SNDMORE)
                    self._socket.send(b"0", zmq.SNDMORE)
                    self._socket.send(packed)
            time.sleep(self.interval)

    def _publish_all_initial_blocks(self):
        if self.is_pool_publisher:
            return  # pool publisher: no blocks, drains offload queue only

        # Collect pre-generated blocks in order.
        items: list[tuple[int, int, list[int]]] = []
        with self._lock:
            for _seq, (th, tokens) in list(self._active_blocks.items()):
                items.append((_seq, th, tokens))
            self._stats_stored = len(items)
            self._total_pub += len(items)
        if not items:
            return

        # Publish in BATCH_SIZE chunks so each chunk starts a new chain
        # from root.  A single giant chain would bury later block indices
        # deep in the tree where queries cannot find them at root.children.
        BATCH_SIZE = 16
        total_published = 0
        for chunk_start in range(0, len(items), BATCH_SIZE):
            chunk = items[chunk_start : chunk_start + BATCH_SIZE]
            hashes = []
            all_tokens = []
            for _, th, tokens in chunk:
                hashes.append(th)
                all_tokens.extend(tokens)

            with self._lock:
                self._event_id += 1
                eid = self._event_id

            if self.vllm_format:
                if self._last_published_block_hash is not None:
                    event = [
                        "BlockStored",
                        hashes,
                        self._last_published_block_hash,  # parent = prev batch's last hash
                        all_tokens,
                        self.block_size,
                        0,  # lora_id
                        "GPU",  # medium
                        0,  # group_idx
                        "FullAttention",
                    ]
                else:
                    event = [
                        "BlockStored",
                        hashes,
                        all_tokens,
                        self.block_size,
                        0,  # lora_id
                        "GPU",  # medium
                        0,  # group_idx
                        "FullAttention",
                    ]
                # Track last hash of this batch for cross-batch chaining.
                if hashes:
                    self._last_published_block_hash = hashes[-1]
                ts = time.time()
            else:
                blocks_data = [{"block_hash": th, "tokens_hash": th} for _, th, _ in chunk]
                event = {
                    "event_id": eid,
                    "event_type": "stored",
                    "model_name": self.model_name,
                    "tenant_id": self.tenant_id,
                    "dp_rank": self.dp_rank,
                    "block_size": self.block_size,
                    "blocks": blocks_data,
                    "parent_hash": None,
                    "seq_hashes": hashes,
                }
                ts = int(time.time() * 1000)

            payload = msgpack.packb([ts, [event], self.dp_rank])
            self._socket.send(b"", zmq.SNDMORE)
            self._socket.send(b"0", zmq.SNDMORE)
            self._socket.send(payload)
            total_published += len(hashes)

        # After initial burst, stop cross-event chaining so runtime stores
        # (which are interleaved with removes) don't reference evicted parents.
        self._last_published_block_hash = None

        fmt_note = " (vLLM)" if self.vllm_format else ""
        print(
            f"[mock] initial burst published: {total_published} blocks "
            f"in {(total_published + BATCH_SIZE - 1) // BATCH_SIZE} batches "
            f"dp={self.dp_rank}{fmt_note}",
            flush=True,
        )

    # ── HTTP helpers ──────────────────────────────────────────────────

    def register(self, conductor_url: str, medium_endpoints: dict = None):
        if medium_endpoints is not None:
            payload = {
                "instance_id": self.instance_id,
                "medium_endpoints": medium_endpoints,
                "type": "Mooncake",
                "store_backend": "Mooncake",
                "modelname": self.model_name,
                "block_size": self.block_size,
                "dp_rank": self.dp_rank,
                "tenant_id": self.tenant_id,
            }
        else:
            endpoint = _zmq_endpoint(_pod_ip(), self.port)
            payload = {
                "instance_id": self.instance_id,
                "endpoint": endpoint,
                "type": "Mooncake",
                "modelname": self.model_name,
                "block_size": self.block_size,
                "dp_rank": self.dp_rank,
                "tenant_id": self.tenant_id,
            }
        try:
            r = requests.post(f"http://{conductor_url}/register", json=payload, timeout=2)
            if r.status_code in (200, 201):
                with self._lock:
                    self._conductor_url = conductor_url
                    self._registered = True
                proto = "medium_endpoints" if medium_endpoints else "legacy"
                print(f"[mock] Registered {self.instance_id} dp={self.dp_rank} → {conductor_url} ({proto})")
                time.sleep(0.5)
                self._publish_all_initial_blocks()
            else:
                print(f"[mock] Register failed: {r.status_code} {r.text}")
        except requests.RequestException as e:
            print(f"[mock] Register error: {e}")

    def unregister(self):
        if not self._conductor_url:
            print("[mock] Not registered")
            return
        payload = {
            "instance_id": self.instance_id,
            "type": "Mooncake",
            "modelname": self.model_name,
            "block_size": self.block_size,
            "dp_rank": self.dp_rank,
            "tenant_id": self.tenant_id,
        }
        try:
            r = requests.post(f"http://{self._conductor_url}/unregister", json=payload, timeout=2)
            if r.status_code == 200:
                with self._lock:
                    self._registered = False
                print(f"[mock] Unregistered {self.instance_id} dp={self.dp_rank}")
            else:
                print(f"[mock] Unregister failed: {r.status_code} {r.text}")
        except requests.RequestException as e:
            print(f"[mock] Unregister error: {e}")

    def _print_status(self):
        with self._lock:
            reg = self._registered
            pub = self._publishing
        print(f"  instance_id:  {self.instance_id}")
        print(f"  model:        {self.model_name}  dp_rank={self.dp_rank}")
        print(f"  block_size:   {self.block_size}  port=tcp://*:{self.port}")
        print(f"  format:       {'vLLM' if self.vllm_format else 'Mooncake'}  SWA={'on' if self.swa_mixed else 'off'}")
        print(f"  publishing:   {'ON' if pub else 'OFF'} (interval={self.interval}s)")
        print(f"  cache:        {len(self._active_blocks)}/{self._max_blocks} blocks")
        print(f"               (+{self._growth_rate * 100:.0f}% / {self._growth_interval}s)")
        print(
            f"  events sent:  {self._event_id} (stored={self._stats_stored} "
            f"removed={self._stats_removed} cleared={self._stats_cleared})"
        )
        print(f"  registered:   {reg} → {self._conductor_url or 'N/A'}")

    def _cli_loop(self):
        print("\n" + "=" * 60)
        if not sys.stdin.isatty():
            print("[mock] No TTY, headless mode")
            while self._running:
                time.sleep(10)
            return

        print("Mock ZMQ Publisher — Interactive Mode")
        print("Commands: register <url> | unregister | status | start | stop | quit")
        print("=" * 60)
        while self._running:
            try:
                line = input("\n[mock] > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()
            if cmd == "register" and len(parts) >= 2:
                self.register(parts[1])
            elif cmd == "unregister":
                self.unregister()
            elif cmd == "status":
                self._print_status()
            elif cmd == "start":
                self._publishing = True
                print("[mock] Publishing resumed")
            elif cmd == "stop":
                self._publishing = False
                print("[mock] Publishing paused")
            elif cmd in ("quit", "exit"):
                break
            else:
                print("Commands: register <url> | unregister | status | start | stop | quit")

    def run(self):
        t = threading.Thread(target=self._publish_loop, daemon=True)
        t.start()
        self._cli_loop()
        self._running = False
        self._socket.close()
        self._ctx.term()
        print("[mock] Shutdown complete")


# ---------------------------------------------------------------------------
# Multi-port runner — separate publishers per medium, single registration
# ---------------------------------------------------------------------------


class MultiPortRunner:
    """Runs two MockZmqPublisher instances on separate ports, one for XPU/HBM
    and one for CPU+DISK, registering them jointly via medium_endpoints.
    This is the default mode.
    """

    def __init__(
        self,
        xpu_port: int,
        cpu_disk_port: int,
        model_name: str,
        dp_rank: int,
        block_size: int,
        initial_blocks: int,
        interval: float,
        instance_id: str = None,
        tenant_id: str = "default",
        conductor_url: str = None,
        store_backend: str = "Mooncake",
        backend_id: str = "",
        vllm_format: bool = True,
        swa_mixed: bool = True,
    ):
        self.xpu_port = xpu_port
        self.cpu_disk_port = cpu_disk_port
        self.model_name = model_name
        self.dp_rank = dp_rank
        self.block_size = block_size
        self.tenant_id = tenant_id
        self.instance_id = instance_id or f"mock-{model_name}-dp{dp_rank}"
        self.conductor_url = conductor_url
        self.store_backend = store_backend
        bid = backend_id or _pod_ip()

        # Shared offload queue: XPU publisher pushes offloaded block hashes;
        # CPU/DISK publisher drains them to emit Mooncake confirm events.
        offload_queue: list = []

        pub_kwargs = dict(
            model_name=model_name,
            dp_rank=dp_rank,
            block_size=block_size,
            initial_blocks=initial_blocks,
            interval=interval,
            instance_id=instance_id,
            tenant_id=tenant_id,
            backend_id=bid,
            vllm_format=vllm_format,
            swa_mixed=swa_mixed,
        )
        self._xpu_pub = MockZmqPublisher(
            port=xpu_port,
            offload_queue=offload_queue,
            **pub_kwargs,
        )
        self._cpu_disk_pub = MockZmqPublisher(
            port=cpu_disk_port,
            offload_queue=offload_queue,
            is_pool_publisher=True,
            **pub_kwargs,
        )
        self._running = False

    def register(self, conductor_url: str = None):
        url = conductor_url or self.conductor_url
        if not url:
            print("[multi] ERROR: conductor URL required for registration")
            return

        pod_ip = _pod_ip()
        medium_endpoints = {
            "xpu": _zmq_endpoint(pod_ip, self.xpu_port),
            "cpu": _zmq_endpoint(pod_ip, self.cpu_disk_port),
            "disk": _zmq_endpoint(pod_ip, self.cpu_disk_port),
        }

        self._xpu_pub.register(url, medium_endpoints=medium_endpoints)

        with self._cpu_disk_pub._lock:
            self._cpu_disk_pub._conductor_url = url
            self._cpu_disk_pub._registered = True
        time.sleep(0.3)
        self._cpu_disk_pub._publish_all_initial_blocks()

        print(f"[multi] Both ports registered: XPU=:{self.xpu_port}, CPU+DISK=:{self.cpu_disk_port}")
        print(f"       medium_endpoints={medium_endpoints}")

    def run(self):
        self._running = True
        t_xpu = threading.Thread(target=self._xpu_pub._publish_loop, daemon=True)
        t_xpu.start()
        t_cpu = threading.Thread(target=self._cpu_disk_pub._publish_loop, daemon=True)
        t_cpu.start()

        print("\n" + "=" * 60)
        print("Multi-Port Mock ZMQ Publisher (two-phase offload simulation)")
        print(f"  XPU port:     tcp://*:{self.xpu_port}  (vLLM BlockStored, ~20% offloaded)")
        print(f"  CPU+DISK port: tcp://*:{self.cpu_disk_port}  (Mooncake pool confirm)")
        print(f"  model:        {self.model_name}  dp_rank={self.dp_rank}")
        print(f"  instance_id:  {self.instance_id}")
        print("  Flow: XPU emits cpu-block → cached → CPU port confirms → tree insert")
        print("Commands: register <url> | unregister | status | quit")
        print("=" * 60)

        if not sys.stdin.isatty():
            print("[multi] No TTY, headless mode — register and publish indefinitely")
            if self.conductor_url:
                self.register(self.conductor_url)
            while self._running:
                time.sleep(10)
            return

        while self._running:
            try:
                line = input("\n[multi] > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()
            if cmd == "register" and len(parts) >= 2:
                self.register(parts[1])
            elif cmd == "unregister":
                self._xpu_pub.unregister()
            elif cmd == "status":
                queue_len = len(self._xpu_pub.offload_queue)
                print(
                    f"  XPU publisher:      port={self.xpu_port}  "
                    f"active={len(self._xpu_pub._active_blocks)}/{self._xpu_pub._max_blocks}"
                )
                print(f"  CPU+DISK publisher: port={self.cpu_disk_port}  pending_confirm={queue_len}")
                print("  Two-phase flow: XPU offload → cache → CPU port confirm → tree insert")
            elif cmd in ("quit", "exit"):
                break
            else:
                print("Commands: register <url> | unregister | status | quit")

        self._running = False
        self._xpu_pub._running = False
        self._cpu_disk_pub._running = False
        for pub in (self._xpu_pub, self._cpu_disk_pub):
            pub._socket.close()
            pub._ctx.term()
        print("[multi] Shutdown complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="Mock ZMQ KV Event Publisher")
    # Ports
    p.add_argument("--xpu-port", type=int, default=15557, help="XPU/HBM ZMQ PUB port")
    p.add_argument("--cpu-disk-port", type=int, default=15558, help="CPU+DISK ZMQ PUB port")
    # Single-port mode
    p.add_argument("--single-port", action="store_true", help="Single-port mode (legacy; multi-port is default)")
    p.add_argument("--port", type=int, default=5557, help="ZMQ PUB port (single-port mode only)")
    # Model
    p.add_argument("--model", type=str, default="opt-125m")
    p.add_argument("--dp-rank", type=int, default=0)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--initial-blocks", type=int, default=8192, help="Initial cache capacity (grows ~30%%/2min)")
    p.add_argument("--interval", type=float, default=2.0, help="Publish interval in seconds")
    p.add_argument("--instance-id", type=str, default=None)
    p.add_argument("--tenant-id", type=str, default="default")
    # Format (opt-out flags)
    p.add_argument(
        "--mooncake-format", action="store_true", help="Use legacy Mooncake wire format (default: vLLM msgspec)"
    )
    p.add_argument("--no-swa-mixed", action="store_true", help="Disable SWA attention events mixing (default: mixed)")
    # Registration
    p.add_argument(
        "--store-backend", type=str, default="Mooncake", help="KV storage backend type: Mooncake, Memcache, or YuanRong"
    )
    p.add_argument(
        "--backend-id", type=str, default="", help="backend_id for events (node IP, default: POD_IP env or 127.0.0.1)"
    )
    p.add_argument("--conductor-url", type=str, default=None, help="Auto-register with conductor on startup")
    args = p.parse_args()

    vllm_fmt = not args.mooncake_format
    swa = not args.no_swa_mixed

    if args.single_port:
        pub = MockZmqPublisher(
            port=args.port,
            model_name=args.model,
            dp_rank=args.dp_rank,
            block_size=args.block_size,
            initial_blocks=args.initial_blocks,
            interval=args.interval,
            instance_id=args.instance_id,
            tenant_id=args.tenant_id,
            backend_id=args.backend_id,
            vllm_format=vllm_fmt,
            swa_mixed=swa,
        )
        pub.run()
    else:
        runner = MultiPortRunner(
            xpu_port=args.xpu_port,
            cpu_disk_port=args.cpu_disk_port,
            model_name=args.model,
            dp_rank=args.dp_rank,
            block_size=args.block_size,
            initial_blocks=args.initial_blocks,
            interval=args.interval,
            instance_id=args.instance_id,
            tenant_id=args.tenant_id,
            conductor_url=args.conductor_url,
            store_backend=args.store_backend,
            backend_id=args.backend_id,
            vllm_format=vllm_fmt,
            swa_mixed=swa,
        )
        runner.run()


if __name__ == "__main__":
    main()
