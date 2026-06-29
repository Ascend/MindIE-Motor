"""
Deterministic token generator for mock publisher and bench queries.

Uses XXH3 (seed=42) to produce unique token sequences per
(dp_rank, block_index, position_in_block) tuple — no global pool,
no cycling, no hash collision between different blocks.
"""

import xxhash


def generate_tokens(dp_rank: int, block_size: int, block_index: int):
    """Return a list of *block_size* token IDs deterministically.

    Each (dp_rank, block_index, pos) tuple produces a unique token via
    XXH3(str(key), seed=42) mod 50000 + 100, mimicking a 50k vocab.
    Different blocks always produce different sequences.
    """
    tokens = []
    for pos in range(block_size):
        key = f"{dp_rank}:{block_index}:{pos}".encode()
        token = xxhash.xxh3_64_intdigest(key, 42) % 50000 + 100
        tokens.append(token)
    return tokens


# Backward-compat shim: expose a large-enough pool for callers
# that still index by position (conductor_cli.sh bench).
TOKEN_POOL_SIZE = 200000
TOKEN_POOL = [
    xxhash.xxh3_64_intdigest(str(i).encode(), 42) % 50000 + 100
    for i in range(TOKEN_POOL_SIZE)
]
