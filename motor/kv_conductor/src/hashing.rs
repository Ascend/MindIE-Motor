// SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-FileCopyrightText: Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
//
// This file is a Derivative Work of NVIDIA Dynamo kv-router hashing helpers
// in lib/kv-router/src/protocols.rs (XXH3_SEED, compute_block_hash,
// little-endian xxh3 block hashing), originally licensed under the Apache
// License, Version 2.0. Upstream project: https://github.com/ai-dynamo/dynamo
//
// You may obtain a copy of the Apache License at:
//   http://www.apache.org/licenses/LICENSE-2.0
// Local copy: licenses/Apache-2.0.txt
// Attribution: THIRD_PARTY_NOTICES.md
//
// Modified by Huawei Technologies Co., Ltd. for MindIE-PyMotor KV Conductor
// (i64 token input with per-chunk conversion, rayon parallel batching,
// include partial trailing block via div_ceil). Huawei modifications are
// also available under Mulan PSL v2 (http://license.coscl.org.cn/MulanPSL2).
// Redistribution of this file must still comply with Apache License 2.0.

//! XXH3-based token block hashing.
//!
//! Derived from NVIDIA Dynamo kv-router hashing helpers in
//! `lib/kv-router/src/protocols.rs` (Apache-2.0). See `THIRD_PARTY_NOTICES.md`.
//!
//! Computes `LocalBlockHash` values from token sequences using a sliding-window

use xxhash_rust::xxh3;

use crate::identity::CacheIdentity;
use crate::protocols::LocalBlockHash;

/// Seed for XXH3 hashing (same value as NVIDIA Dynamo kv-router for wire
/// interoperability).
pub const XXH3_SEED: u64 = 1337;

/// Compute the hash of arbitrary data.
#[inline]
pub fn compute_block_hash(data: &[u8]) -> LocalBlockHash {
    LocalBlockHash(xxh3::xxh3_64_with_seed(data, XXH3_SEED))
}

/// Mix a validated identity into the complete prefix's first local hash.
pub fn compute_identity_root_hash(
    identity: &CacheIdentity,
    token_hash: LocalBlockHash,
) -> LocalBlockHash {
    let mut bytes = b"kv-conductor/root-identity/v1\0".to_vec();
    identity.encode_into(&mut bytes);
    bytes.extend_from_slice(&token_hash.0.to_le_bytes());
    compute_block_hash(&bytes)
}

/// Minimum number of blocks to trigger parallel hash computation via rayon.
/// With multi-core pods (>=4 CPU), parallel is beneficial above ~2048 blocks.
/// Below this, sequential is fast enough (<1ms) and avoids rayon overhead.
const PAR_THRESHOLD: usize = 2048;

/// Number of blocks per batch for parallel processing.
/// Each rayon task processes this many blocks sequentially, avoiding
/// excessive task-spawning overhead. For 50600 blocks → ~50 tasks.
const PAR_BATCH_BLOCKS: usize = 1024;

/// Compute block hashes for a sequence of tokens using a sliding window of
/// `block_size` tokens. Each window produces one `LocalBlockHash`.
///
/// Tokens are converted from i64 to u32 per-chunk (not pre-allocated for the
/// entire sequence), avoiding a 512KB+ intermediate allocation for large
/// sequences (e.g. DeepSeek V4 128K tokens).
pub fn compute_block_hash_for_seq(tokens: &[i64], block_size: u32) -> Vec<LocalBlockHash> {
    if block_size == 0 {
        return Vec::new();
    }

    let stride = block_size as usize;
    let estimated_blocks = tokens.len().div_ceil(stride);

    if estimated_blocks <= PAR_THRESHOLD {
        hash_chunks_sequential(tokens, stride, estimated_blocks)
    } else {
        hash_chunks_parallel(tokens, stride)
    }
}

/// Hash each chunk on a single thread, converting i64→u32 per chunk.
#[inline]
fn hash_chunks_sequential(
    tokens: &[i64],
    stride: usize,
    estimated_blocks: usize,
) -> Vec<LocalBlockHash> {
    let mut hashes = Vec::with_capacity(estimated_blocks);
    for chunk in tokens.chunks(stride) {
        hashes.push(hash_i64_chunk(chunk));
    }
    hashes
}

/// Hash chunks in parallel using rayon, processing blocks in batches.
/// Converts i64→u32 per chunk to avoid a full-sequence intermediate allocation.
fn hash_chunks_parallel(tokens: &[i64], stride: usize) -> Vec<LocalBlockHash> {
    use rayon::prelude::*;
    let batch_elems = stride * PAR_BATCH_BLOCKS;
    tokens
        .par_chunks(batch_elems)
        .flat_map(|batch| {
            let b_stride = stride.min(batch.len());
            let count = batch.len().div_ceil(b_stride);
            let mut hashes = Vec::with_capacity(count);
            for chunk in batch.chunks(b_stride) {
                hashes.push(hash_i64_chunk(chunk));
            }
            hashes
        })
        .collect()
}

/// Hash a single chunk of i64 tokens: convert to u32, then hash as raw bytes.
#[inline]
fn hash_i64_chunk(chunk: &[i64]) -> LocalBlockHash {
    // Per-chunk conversion avoids the 512KB+ allocation for full sequence.
    let u32s: Vec<u32> = chunk.iter().map(|&t| t as u32).collect();
    hash_u32_chunk(&u32s)
}

/// Hash a single chunk of u32 tokens as raw bytes (little-endian).
#[inline]
fn hash_u32_chunk(chunk: &[u32]) -> LocalBlockHash {
    #[cfg(target_endian = "little")]
    {
        let chunk_bytes = unsafe {
            std::slice::from_raw_parts(chunk.as_ptr().cast::<u8>(), std::mem::size_of_val(chunk))
        };
        LocalBlockHash(xxh3::xxh3_64_with_seed(chunk_bytes, XXH3_SEED))
    }

    #[cfg(not(target_endian = "little"))]
    {
        let mut bytes = Vec::with_capacity(chunk.len() * 4);
        for &token in chunk {
            bytes.extend_from_slice(&token.to_le_bytes());
        }
        LocalBlockHash(xxh3::xxh3_64_with_seed(&bytes, XXH3_SEED))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::AdapterIdentity;

    #[test]
    fn identity_root_matches_independent_libxxhash_vectors() {
        // Generated with libxxhash's C XXH3_64bits_withSeed, including UTF-8 lengths.
        for (name, salt, token, expected) in [
            (None, None, 0, 0x471a_c7ae_f774_1ee1),
            (
                Some("sql-helper@v1"),
                Some("session-123"),
                123,
                0xb04e_4212_a4d6_9c5d,
            ),
            (
                Some("适配器"),
                Some("盐"),
                0xfedc_ba98_7654_3210,
                0xb59f_8b85_3765_2d62,
            ),
        ] {
            let identity = CacheIdentity {
                schema_version: 1,
                adapter: name.map_or(AdapterIdentity::Base, |name| AdapterIdentity::Lora {
                    name: name.into(),
                }),
                cache_salt: salt.map(str::to_owned),
            };
            assert_eq!(
                compute_identity_root_hash(&identity, LocalBlockHash(token)),
                LocalBlockHash(expected)
            );
        }
    }

    #[test]
    fn identity_root_encoding_separates_adapter_salt_and_token_hash() {
        let raw = compute_block_hash_for_seq(&[1, 2, 3, 4], 4)[0];
        let mut roots = std::collections::HashSet::new();
        for adapter in [
            AdapterIdentity::Base,
            AdapterIdentity::Lora { name: "a".into() },
            AdapterIdentity::Lora { name: "A".into() },
        ] {
            for salt in [None, Some("a"), Some("A")] {
                let identity = CacheIdentity {
                    schema_version: 1,
                    adapter: adapter.clone(),
                    cache_salt: salt.map(str::to_owned),
                };
                let root = compute_identity_root_hash(&identity, raw);
                assert_ne!(root, raw);
                assert_eq!(root, compute_identity_root_hash(&identity, raw));
                assert_ne!(
                    root,
                    compute_identity_root_hash(&identity, LocalBlockHash(raw.0 + 1))
                );
                assert!(roots.insert(root));
            }
        }
        let a = CacheIdentity {
            schema_version: 1,
            adapter: AdapterIdentity::Lora { name: "a".into() },
            cache_salt: Some("bc".into()),
        };
        let b = CacheIdentity {
            schema_version: 1,
            adapter: AdapterIdentity::Lora { name: "ab".into() },
            cache_salt: Some("c".into()),
        };
        assert_ne!(
            compute_identity_root_hash(&a, raw),
            compute_identity_root_hash(&b, raw)
        );
    }

    #[test]
    fn test_compute_block_hash_empty() {
        let hashes = compute_block_hash_for_seq(&[], 4);
        assert!(hashes.is_empty());
    }

    #[test]
    fn test_compute_block_hash_zero_block_size() {
        let hashes = compute_block_hash_for_seq(&[1, 2, 3, 4], 0);
        assert!(hashes.is_empty());
    }

    #[test]
    fn test_compute_block_hash_exact_block() {
        let tokens: Vec<i64> = vec![0, 1, 2, 3];
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        assert_eq!(hashes.len(), 1);
    }

    #[test]
    fn test_compute_block_hash_partial_block() {
        let tokens: Vec<i64> = vec![0, 1, 2, 3, 4, 5];
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        // 6 tokens / 4 stride = ceil(1.5) = 2 blocks
        assert_eq!(hashes.len(), 2);
    }

    #[test]
    fn test_compute_block_hash_deterministic() {
        let tokens: Vec<i64> = vec![100, 200, 300, 400];
        let h1 = compute_block_hash_for_seq(&tokens, 4);
        let h2 = compute_block_hash_for_seq(&tokens, 4);
        assert_eq!(h1[0], h2[0]);
    }

    #[test]
    fn test_different_sequences_produce_different_hashes() {
        let t1: Vec<i64> = vec![1, 2, 3, 4];
        let t2: Vec<i64> = vec![1, 2, 3, 5];
        let h1 = compute_block_hash_for_seq(&t1, 4);
        let h2 = compute_block_hash_for_seq(&t2, 4);
        assert_ne!(h1[0], h2[0]);
    }

    #[test]
    fn test_many_blocks() {
        let tokens: Vec<i64> = (0..1000).collect();
        let hashes = compute_block_hash_for_seq(&tokens, 128);
        // 1000 / 128 = 7.8 -> 8 blocks
        assert_eq!(hashes.len(), 8);
    }

    /// Sequential and parallel paths must produce identical results.
    #[test]
    fn test_sequential_and_parallel_produce_same_hashes() {
        let tokens: Vec<i64> = (0..2000).collect(); // 2000/4 = 500 blocks → parallel
        let h_auto = compute_block_hash_for_seq(&tokens, 4);
        let h_seq = hash_chunks_sequential(&tokens, 4, tokens.len().div_ceil(4));
        assert_eq!(h_auto, h_seq);
    }

    /// Small sequences stay sequential (below PAR_THRESHOLD).
    #[test]
    fn test_small_sequence_stays_sequential() {
        // 100 tokens / 4 = 25 blocks < 2048 threshold
        let tokens: Vec<i64> = (0..100).collect();
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        assert_eq!(hashes.len(), 25);
    }

    /// Large sequences trigger parallel path (above PAR_THRESHOLD).
    #[test]
    fn test_large_sequence_triggers_parallel() {
        // 10000 tokens / 4 = 2500 blocks > 2048 threshold
        let tokens: Vec<i64> = (0..10000).collect();
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        assert_eq!(hashes.len(), 2500);
    }

    /// DeepSeek V4 style: 128K tokens, block_size=4.
    #[test]
    fn test_deepseek_v4_style_large_sequence() {
        let tokens: Vec<i64> = (0i64..131072).collect(); // 128K tokens
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        assert_eq!(hashes.len(), 32768); // 131072 / 4
    }

    /// hash_u32_chunk with a partial block (last chunk shorter than stride).
    #[test]
    fn test_hash_u32_chunk_partial() {
        let chunk: Vec<u32> = vec![1, 2, 3];
        let h = hash_u32_chunk(&chunk);
        let chunk4: Vec<u32> = vec![1, 2, 3, 4];
        let h4 = hash_u32_chunk(&chunk4);
        // Different token sequences → different hashes
        assert_ne!(h, h4);
    }
}
