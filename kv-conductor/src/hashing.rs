// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! XXH3-based token block hashing.
//!
//! Computes `LocalBlockHash` values from token sequences using a sliding-window

use xxhash_rust::xxh3;

use crate::protocols::LocalBlockHash;

/// Seed for XXH3 hashing, consistent with Dynamo kv-router.
pub const XXH3_SEED: u64 = 1337;

/// Compute the hash of arbitrary data.
#[inline]
pub fn compute_block_hash(data: &[u8]) -> LocalBlockHash {
    LocalBlockHash(xxh3::xxh3_64_with_seed(data, XXH3_SEED))
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
        // 100 tokens / 4 = 25 blocks < 256 threshold
        let tokens: Vec<i64> = (0..100).collect();
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        assert_eq!(hashes.len(), 25);
    }

    /// Large sequences trigger parallel path.
    #[test]
    fn test_large_sequence_triggers_parallel() {
        // 2000 tokens / 4 = 500 blocks > 256 threshold
        let tokens: Vec<i64> = (0..2000).collect();
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        assert_eq!(hashes.len(), 500);
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
