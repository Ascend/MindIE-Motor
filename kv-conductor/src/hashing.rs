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
//! approach matching Dynamo's `compute_block_hash_for_seq`.

use xxhash_rust::xxh3;

use crate::protocols::LocalBlockHash;

/// Seed for XXH3 hashing, consistent with Dynamo kv-router.
pub const XXH3_SEED: u64 = 1337;

/// Compute the hash of arbitrary data.
#[inline]
pub fn compute_block_hash(data: &[u8]) -> LocalBlockHash {
    LocalBlockHash(xxh3::xxh3_64_with_seed(data, XXH3_SEED))
}

/// Compute block hashes for a sequence of tokens using a sliding window of
/// `block_size` tokens. Each window produces one `LocalBlockHash`.
///
/// Tokens that are `i64` (from Python JSON) are converted to `u32` via `as u32`.
/// For typical LLM tokenizers, token IDs are well within u16 range.
pub fn compute_block_hash_for_seq(tokens: &[i64], block_size: u32) -> Vec<LocalBlockHash> {
    if block_size == 0 {
        return Vec::new();
    }

    let stride = block_size as usize;
    let estimated_blocks = tokens.len().div_ceil(stride);
    let mut hashes = Vec::with_capacity(estimated_blocks);

    // Convert i64 tokens to u32 for hashing
    let tokens_u32: Vec<u32> = tokens.iter().map(|t| *t as u32).collect();

    for chunk in tokens_u32.chunks(stride) {
        // On little-endian targets, reinterpret u32 slice as u8 bytes directly
        #[cfg(target_endian = "little")]
        {
            let chunk_bytes = unsafe {
                std::slice::from_raw_parts(
                    chunk.as_ptr().cast::<u8>(),
                    std::mem::size_of_val(chunk),
                )
            };
            hashes.push(LocalBlockHash(xxh3::xxh3_64_with_seed(
                chunk_bytes,
                XXH3_SEED,
            )));
        }

        #[cfg(not(target_endian = "little"))]
        {
            let mut bytes = Vec::with_capacity(chunk.len() * 4);
            for &token in chunk {
                bytes.extend_from_slice(&token.to_le_bytes());
            }
            hashes.push(LocalBlockHash(xxh3::xxh3_64_with_seed(&bytes, XXH3_SEED)));
        }
    }

    hashes
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
}
