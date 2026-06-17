#!/usr/bin/env python3
"""
Mock ZMQ KV Event Publisher for kv-conductor e2e testing.

Simulates realistic engine KV cache behavior:
  - Store / Remove / Clear events with realistic ratios (~80% / ~15% / ~5%)
  - Total blocks grow ~5% every 2 minutes, mimicking accumulating cache
  - Token IDs drawn from tokenizer-like distributions (frequent / mid / rare)
  - Blocks form parent→child chains with real XXH3 hashing

Binds a ZMQ PUB socket and broadcasts in Mooncake msgpack wire format.
Supports an interactive CLI for registering with kv-conductor.

Usage:
    python zmq_publisher.py --port 5557 --model opt-125m --dp-rank 0 --block-size 128
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
        buf = struct.pack(f'<{len(chunk)}I', *chunk)
        hashes.append(xxhash.xxh3_64_intdigest(buf, SEED))
    return hashes


# ---------------------------------------------------------------------------
# Realistic token generation
# ---------------------------------------------------------------------------

# Shared token pool — same pool used by bench for realistic queries.
# Each publisher (dp_rank) uses a different offset into this pool so
# queries with common tokens can actually hit cached blocks across DPs.
# Mix of frequent (100-3000), mid (3000-25000), rare (25000-50000).
# fmt: off
# noqa — shared token pool, intentionally kept as-is
_TOKEN_POOL = [
    101, 2023, 318, 559, 234, 1039, 640, 1024, 286,
    562, 317, 859, 1053, 1288, 2000, 345, 678, 901,
    1234, 1567, 1890, 2123, 2456, 2789, 3012, 3345, 3678,
    4001, 4334, 4667, 4999, 5321, 5654, 5987, 6319, 6652,
    6985, 7318, 7650, 7983, 8316, 8649, 8981, 9314, 9647,
    9980, 10313, 10646, 10979, 11312, 11645, 11978, 12311,
    12644, 12977, 13310, 13643, 13976, 14309, 14642, 14975,
    15308, 15641, 15974, 16307, 16640, 16973, 17306, 17639,
    17972, 18305, 18638, 18971, 19304, 19637, 19970, 20303,
    20636, 20969, 21302, 21635, 21968, 22301, 22634, 22967,
    23300, 23633, 23966, 24299, 24632, 24965, 25298, 25631,
    25964, 26297, 26630, 26963, 27296, 27629, 27962, 28295,
    28628, 28961, 29294, 29627, 29960, 30293, 30626, 30959,
    31292, 31625, 31958, 32291, 32624, 32957, 33290, 33623,
    33956, 34289, 34622, 34955, 35288, 35621, 35954, 36287,
    36620, 36953, 37286, 37619, 37952, 38285, 38618, 38951,
    39284, 39617, 39950, 40283, 40616, 40949, 41282, 41615,
    41948, 42281, 42614, 42947, 43280, 43613, 43946, 44279,
    44612, 44945, 45278, 45611, 45944, 46277, 46610, 46943,
    47276, 47609, 47942, 48275, 98306,
] # fmt: on

def generate_tokens_for_publisher(dp_rank, block_size, block_index):
    """Deterministic tokens from the shared pool at a dp_rank-specific offset.

    Each publisher (dp_rank) starts at a different segment of the pool,
    so queries with common tokens can match any DP's cached blocks.
    """
    offset = (dp_rank * 1000 + block_index * block_size) % len(_TOKEN_POOL)
    tokens = []
    for i in range(block_size):
        tokens.append(_TOKEN_POOL[(offset + i) % len(_TOKEN_POOL)])
    return tokens


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
    ):
        self.port = port
        self.model_name = model_name
        self.dp_rank = dp_rank
        self.block_size = block_size
        self.interval = interval
        self.instance_id = instance_id or f"mock-{model_name}-dp{dp_rank}"
        self.backend_id = backend_id or os.environ.get("POD_IP", "127.0.0.1")
        self.tenant_id = tenant_id

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
        self._rng = random.Random(dp_rank * 12345 + port * 67890)  # nosec B311 — mock data, not cryptographic

        # --- Virtual KV cache ---
        # { seq_hash: (tokens_hash, tokens) }
        self._active_blocks = {}
        # Max capacity — grows by ~5% every GROWTH_INTERVAL seconds
        self._max_blocks = max(initial_blocks, 4)
        self._growth_interval = 120  # 2 minutes
        self._growth_rate = 0.30  # 30% per cycle
        self._growth_timer = time.time()
        self._next_seq = 1

        # --- Pre-generate initial blocks ---
        for _ in range(self._max_blocks * block_size // block_size or 1):
            self._generate_and_store_block()

        # ZMQ
        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.PUB)
        self._socket.bind(f"tcp://*:{port}")
        self._total_pub = 0
        self._total_rem = 0
        print(f"[mock] ZMQ PUB bound on tcp://*:{port}")
        print(
            f"[mock] model={model_name} dp={dp_rank} block_size={block_size} "
            f"init_capacity={self._max_blocks} interval={interval}s"
        )

    # ── Virtual cache operations ──────────────────────────────────────

    def _generate_and_store_block(self):
        """Generate a token block from shared pool and add it to virtual cache."""
        tokens = generate_tokens_for_publisher(self.dp_rank, self.block_size, len(self._active_blocks))
        th = compute_block_hashes(tokens, self.block_size)[0]
        seq = self._next_seq
        self._next_seq += 1
        self._active_blocks[seq] = (th, tokens)
        return seq, th, tokens

    def _evict_block(self):
        """Evict the oldest block from the virtual cache."""
        if not self._active_blocks:
            return None
        oldest_seq = min(self._active_blocks.keys())
        th, tokens = self._active_blocks.pop(oldest_seq)
        return oldest_seq, th, tokens

    # ── Publish cycle ─────────────────────────────────────────────────

    def _build_events(self):
        """Decide what events to publish based on cache state and growth."""
        now = time.time()

        # --- Growth check: +5% every 2 minutes ---
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
            # STORE — add new blocks to fill toward capacity
            return self._make_store_batch(min(3, self._max_blocks - current))
        elif current > max(4, self._max_blocks // 2) and roll < 0.95:
            # REMOVE — evict some blocks (but keep at least half capacity)
            return self._make_remove_batch(min(2, current - self._max_blocks // 2))
        elif roll < 0.995 and current > 0:
            # CLEAR — very rare full clear
            return self._make_clear_event()
        else:
            # Default: store to grow
            return self._make_store_batch(1)

    def _make_store_batch(self, count):
        """Create a Stored event batch with `count` new blocks."""
        if count <= 0:
            return []

        # All blocks in one batch form a parent→child chain
        with self._lock:
            self._event_id += 1
            eid = self._event_id

        blocks = []
        tokens_hashes = []

        for _ in range(count):
            _, th, _ = self._generate_and_store_block()
            blocks.append({"block_hash": th, "tokens_hash": th})
            tokens_hashes.append(th)

        with self._lock:
            self._stats_stored += count
            self._total_pub += count

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
        self._print_publish("STORE", eid, count, tokens_hashes)
        return [[int(time.time() * 1000), [event], self.dp_rank]]

    def _make_remove_batch(self, count):
        """Create a Removed event batch evicting `count` oldest blocks."""
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

        with self._lock:
            self._stats_removed += len(removed)
            self._total_rem += len(removed)

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
        self._print_publish("REMOVE", eid, len(removed_ths), removed_ths)
        return [[int(time.time() * 1000), [event], self.dp_rank]]

    def _make_clear_event(self):
        """Create a Cleared event and reset the virtual cache."""
        old_count = len(self._active_blocks)
        self._active_blocks.clear()
        self._next_seq = 1

        with self._lock:
            self._event_id += 1
            eid = self._event_id
            self._stats_cleared += 1

        event = {
            "event_id": eid,
            "event_type": "cleared",
            "model_name": self.model_name,
            "tenant_id": self.tenant_id,
            "dp_rank": self.dp_rank,
            "backend_id": self.backend_id,
            "block_size": self.block_size,
        }
        self._print_publish("CLEAR", eid, old_count, [])
        return [[int(time.time() * 1000), [event], self.dp_rank]]

    def _print_publish(self, kind, eid, count, hashes):
        """Log a compact publish record that conductor_cli.sh can parse."""
        h_preview = hashes[:3] if hashes else []
        active = len(self._active_blocks)
        capacity = self._max_blocks
        print(
            f"[publish] {kind} batch=#{eid} blocks={count} "
            f"seq_hashes={h_preview} "
            f"cache={active}/{capacity} "
            f"stored={self._stats_stored} removed={self._stats_removed} "
            f"cleared={self._stats_cleared}",
            flush=True,
        )

    # ── Publish loop ──────────────────────────────────────────────────

    def _publish_loop(self):
        """Background thread: build and publish events at regular intervals."""
        print(f"[mock] publish loop started, initial_cache={len(self._active_blocks)}/{self._max_blocks}")
        while self._running:
            if self._publishing:
                for payload in self._build_events():
                    packed = msgpack.packb(payload)
                    self._socket.send(b"", zmq.SNDMORE)
                    self._socket.send(b"0", zmq.SNDMORE)
                    self._socket.send(packed)
            time.sleep(self.interval)

    # ── HTTP helpers ──────────────────────────────────────────────────

    def register(self, conductor_url: str, medium_endpoints: dict = None):
        """Register with the KV conductor.

        Args:
            conductor_url: Conductor host:port (e.g. 'kv-conductor:13333').
            medium_endpoints: If provided, uses the new RFC medium_endpoints
                protocol. Otherwise falls back to legacy single-endpoint.
        """
        if medium_endpoints is not None:
            # New protocol: medium_endpoints map
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
            # Legacy protocol: single endpoint
            endpoint = f"tcp://{os.environ.get('POD_IP', '127.0.0.1')}:{self.port}"
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

    # ── Interactive CLI ───────────────────────────────────────────────

    def _print_status(self):
        with self._lock:
            reg = self._registered
            pub = self._publishing
            stored = self._stats_stored
            removed = self._stats_removed
            cleared = self._stats_cleared
            eid = self._event_id
        print(f"  instance_id:  {self.instance_id}")
        print(f"  model:        {self.model_name}  dp_rank={self.dp_rank}")
        print(f"  block_size:   {self.block_size}  port=tcp://*:{self.port}")
        print(f"  publishing:   {'ON' if pub else 'OFF'} (interval={self.interval}s)")
        print(f"  cache:        {len(self._active_blocks)}/{self._max_blocks} blocks")
        print(f"               (+{self._growth_rate * 100:.0f}% / {self._growth_interval}s)")
        print(f"  events sent:  {eid} (stored={stored} removed={removed} cleared={cleared})")
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
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multi-port runner — separate publishers per medium, single registration
# ---------------------------------------------------------------------------


class MultiPortRunner:
    """Runs two MockZmqPublisher instances on separate ports, one for XPU/HBM
    and one for CPU+DISK, registering them jointly via medium_endpoints.
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
        instance_id: str,
        tenant_id: str,
        conductor_url: str = None,
        store_backend: str = "Mooncake",
        backend_id: str = "",
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
        bid = backend_id or os.environ.get("POD_IP", "127.0.0.1")

        # Create two publishers — one per port
        self._xpu_pub = MockZmqPublisher(
            port=xpu_port,
            model_name=model_name,
            dp_rank=dp_rank,
            block_size=block_size,
            initial_blocks=initial_blocks,
            interval=interval,
            instance_id=instance_id,
            tenant_id=tenant_id,
            backend_id=bid,
        )
        self._cpu_disk_pub = MockZmqPublisher(
            port=cpu_disk_port,
            model_name=model_name,
            dp_rank=dp_rank,
            block_size=block_size,
            initial_blocks=initial_blocks,
            interval=interval,
            instance_id=instance_id,
            tenant_id=tenant_id,
            backend_id=bid,
        )
        self._running = False

    def register(self, conductor_url: str = None):
        """Register both ports jointly via medium_endpoints protocol."""
        url = conductor_url or self.conductor_url
        if not url:
            print("[multi] ERROR: conductor URL required for registration")
            return

        pod_ip = os.environ.get("POD_IP", "127.0.0.1")
        medium_endpoints = {
            "xpu": f"tcp://{pod_ip}:{self.xpu_port}",
            "cpu": f"tcp://{pod_ip}:{self.cpu_disk_port}",
            "disk": f"tcp://{pod_ip}:{self.cpu_disk_port}",
        }

        # Use the XPU publisher's register method with medium_endpoints
        self._xpu_pub.register(url, medium_endpoints=medium_endpoints)

        # Mark CPU+DISK publisher as registered too
        with self._cpu_disk_pub._lock:
            self._cpu_disk_pub._conductor_url = url
            self._cpu_disk_pub._registered = True

        print(f"[multi] Both ports registered: XPU=:{self.xpu_port}, CPU+DISK=:{self.cpu_disk_port}")
        print(f"       medium_endpoints={medium_endpoints}")

    def run(self):
        """Start both publishers' background threads and enter CLI loop."""
        self._running = True

        # Start publish loops in background threads
        t_xpu = threading.Thread(target=self._xpu_pub._publish_loop, daemon=True)
        t_xpu.start()
        t_cpu = threading.Thread(target=self._cpu_disk_pub._publish_loop, daemon=True)
        t_cpu.start()

        # Use the XPU publisher's CLI for interactive control
        print("\n" + "=" * 60)
        print("Multi-Port Mock ZMQ Publisher")
        print(f"  XPU port:     tcp://*:{self.xpu_port}")
        print(f"  CPU+DISK port: tcp://*:{self.cpu_disk_port}")
        print(f"  model:        {self.model_name}  dp_rank={self.dp_rank}")
        print(f"  instance_id:  {self.instance_id}")
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
                print(f"  XPU publisher:      port={self.xpu_port}  active={len(self._xpu_pub._active_blocks)}/{self._xpu_pub._max_blocks}")
                print(f"  CPU+DISK publisher: port={self.cpu_disk_port}  active={len(self._cpu_disk_pub._active_blocks)}/{self._cpu_disk_pub._max_blocks}")
            elif cmd in ("quit", "exit"):
                break
            else:
                print("Commands: register <url> | unregister | status | quit")

        # Cleanup
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
    p.add_argument("--port", type=int, default=5557, help="ZMQ PUB port (single-port mode)")
    p.add_argument("--model", type=str, default="opt-125m")
    p.add_argument("--dp-rank", type=int, default=0)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--initial-blocks", type=int, default=8, help="Initial cache capacity (grows ~5%%/2min)")
    p.add_argument("--interval", type=float, default=2.0, help="Publish interval in seconds")
    p.add_argument("--instance-id", type=str, default=None)
    p.add_argument("--tenant-id", type=str, default="default")

    # Multi-port mode
    p.add_argument("--multi-port", action="store_true",
                   help="Enable multi-port mode: separate ports for XPU and CPU+DISK")
    p.add_argument("--xpu-port", type=int, default=15557, help="XPU/HBM ZMQ PUB port (multi-port mode)")
    p.add_argument("--cpu-disk-port", type=int, default=15558, help="CPU+DISK ZMQ PUB port (multi-port mode)")
    p.add_argument("--store-backend", type=str, default="Mooncake",
                   help="KV storage backend type: Mooncake, Memcache, or YuanRong")
    p.add_argument("--backend-id", type=str, default="",
                   help="backend_id for events (node IP, default: POD_IP env or 127.0.0.1)")
    p.add_argument("--conductor-url", type=str, default=None,
                   help="Auto-register with conductor on startup (multi-port mode)")
    args = p.parse_args()

    if args.multi_port:
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
        )
        runner.run()
    else:
        pub = MockZmqPublisher(
            port=args.port,
            model_name=args.model,
            dp_rank=args.dp_rank,
            block_size=args.block_size,
            initial_blocks=args.initial_blocks,
            interval=args.interval,
            instance_id=args.instance_id,
            tenant_id=args.tenant_id,
        )
        pub.run()


if __name__ == "__main__":
    main()
