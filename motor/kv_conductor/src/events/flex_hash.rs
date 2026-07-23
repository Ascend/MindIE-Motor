// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! `FlexHash` — polymorphic u64 deserialization for msgpack values.
//!
//! Supports multiple representations:
//!   - integer (u64, i64, u32, …)
//!   - decimal string   "12345678901234567890"
//!   - hex string       "0xABCD1234…" or "ABCD1234…"
//!   - binary bytes     up to 8 bytes, big-endian (vLLM BlockHash compat)

use serde::Deserialize;

/// A u64 that can be deserialized from multiple msgpack representations.
#[derive(Debug, Clone, Copy)]
pub(crate) struct FlexHash(pub(crate) u64);

impl<'de> Deserialize<'de> for FlexHash {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct FlexHashVisitor;
        impl<'de> serde::de::Visitor<'de> for FlexHashVisitor {
            type Value = FlexHash;

            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a u64, decimal/hex string, or up to 8 bytes")
            }

            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<FlexHash, E> {
                Ok(FlexHash(v))
            }

            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<FlexHash, E> {
                if v < 0 {
                    return Err(E::custom(format!("negative hash: {v}")));
                }
                Ok(FlexHash(v as u64))
            }

            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<FlexHash, E> {
                let s = v.trim();
                if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
                    return u64::from_str_radix(hex, 16)
                        .map(FlexHash)
                        .map_err(|e| E::custom(format!("invalid hex hash '{v}': {e}")));
                }
                if let Ok(n) = s.parse::<u64>() {
                    return Ok(FlexHash(n));
                }
                u64::from_str_radix(s, 16).map(FlexHash).map_err(|_| {
                    E::custom(format!(
                        "invalid hash '{v}': expected u64, hex, or 0x-prefixed"
                    ))
                })
            }

            fn visit_bytes<E: serde::de::Error>(self, v: &[u8]) -> Result<FlexHash, E> {
                bytes_to_flex(v)
            }

            fn visit_byte_buf<E: serde::de::Error>(self, v: Vec<u8>) -> Result<FlexHash, E> {
                bytes_to_flex(&v)
            }
        }

        fn bytes_to_flex<E: serde::de::Error>(v: &[u8]) -> Result<FlexHash, E> {
            if v.len() > 8 {
                return Err(E::custom(format!("hash bytes too long: {} > 8", v.len())));
            }
            let mut buf = [0u8; 8];
            buf[8 - v.len()..].copy_from_slice(v);
            Ok(FlexHash(u64::from_be_bytes(buf)))
        }

        deserializer.deserialize_any(FlexHashVisitor)
    }
}
