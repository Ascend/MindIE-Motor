// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Strict v1 wire evidence, kept separately from legacy's permissive decoding.

use rmpv::Value;

use crate::identity::{AdapterIdentity, CacheIdentity, EngineBlockHash};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum WireField<T> {
    Missing,
    Null,
    Value(T),
}

fn wire_field<T>(
    value: Option<&Value>,
    parse: impl FnOnce(&Value) -> Option<T>,
) -> Option<WireField<T>> {
    match value {
        None => Some(WireField::Missing),
        Some(Value::Nil) => Some(WireField::Null),
        Some(value) => parse(value).map(WireField::Value),
    }
}

pub(super) fn engine_hash(value: &Value) -> Option<EngineBlockHash> {
    match value {
        Value::Integer(value) => value.as_u64().map(EngineBlockHash::U64),
        Value::Binary(value) if !value.is_empty() => Some(EngineBlockHash::from_bytes(value)),
        Value::String(value) => {
            let value = value.as_str()?.trim();
            if let Ok(value) = value.parse::<u64>() {
                return Some(EngineBlockHash::U64(value));
            }
            let hex = value
                .strip_prefix("0x")
                .or_else(|| value.strip_prefix("0X"))
                .unwrap_or(value);
            if hex.is_empty() || !hex.bytes().all(|c| c.is_ascii_hexdigit()) {
                return None;
            }
            if hex.len() <= 16 {
                return u64::from_str_radix(hex, 16).ok().map(EngineBlockHash::U64);
            }
            if hex.len() % 2 != 0 {
                return None;
            }
            let bytes = (0..hex.len())
                .step_by(2)
                .map(|start| u8::from_str_radix(&hex[start..start + 2], 16).ok())
                .collect::<Option<Vec<_>>>()?;
            Some(EngineBlockHash::from_bytes(&bytes))
        }
        _ => None,
    }
}

fn engine_hashes(value: &Value) -> Option<Vec<EngineBlockHash>> {
    value.as_array()?.iter().map(engine_hash).collect()
}

fn string(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}

fn optional_value<T>(
    value: Option<&Value>,
    parse: impl FnOnce(&Value) -> Option<T>,
) -> Option<Option<T>> {
    match value {
        None | Some(Value::Nil) => Some(None),
        Some(value) => parse(value).map(Some),
    }
}

fn block_size(value: &Value) -> Option<u32> {
    value.as_u64().and_then(|value| value.try_into().ok())
}

#[derive(Debug, Clone)]
pub(crate) struct IdentityStoredEvent {
    pub(super) block_hashes: Vec<EngineBlockHash>,
    pub(super) parent: WireField<EngineBlockHash>,
    pub(super) token_ids: Vec<i64>,
    pub(super) block_size: u32,
    pub(super) medium: Option<String>,
    pub(super) lora_name: WireField<String>,
    pub(super) extra_keys: Option<Vec<Value>>,
    pub(super) spec_kind: Option<String>,
}

impl IdentityStoredEvent {
    pub(super) fn from_fields<'a>(get: impl Fn(&str) -> Option<&'a Value>) -> Option<Self> {
        Some(Self {
            block_hashes: engine_hashes(get("block_hashes")?)?,
            parent: wire_field(get("parent_block_hash"), engine_hash)?,
            token_ids: get("token_ids")?
                .as_array()?
                .iter()
                .map(Value::as_i64)
                .collect::<Option<_>>()?,
            block_size: block_size(get("block_size")?)?,
            medium: optional_value(get("medium"), string)?,
            lora_name: wire_field(get("lora_name"), string)?,
            extra_keys: optional_value(get("extra_keys"), |value| value.as_array().cloned())?,
            spec_kind: optional_value(get("kv_cache_spec_kind"), string)?,
        })
    }

    /// Known tagged-array layout. Interior nulls retain their positions;
    /// legacy arrays that elide them cannot establish v1 identity evidence.
    pub(super) fn from_values(values: &[Value]) -> Option<Self> {
        Self::from_fields(|name| {
            let index = match name {
                "block_hashes" => 0,
                "parent_block_hash" => 1,
                "token_ids" => 2,
                "block_size" => 3,
                "medium" => 5,
                "lora_name" => 6,
                "extra_keys" => 7,
                "kv_cache_spec_kind" => 9,
                _ => return None,
            };
            values.get(index)
        })
    }

    pub(super) fn main_attention(&self) -> bool {
        let Some(kind) = &self.spec_kind else {
            return false;
        };
        let normalized: String = kind
            .chars()
            .filter(|c| *c != '_')
            .flat_map(char::to_lowercase)
            .collect();
        matches!(
            normalized.as_str(),
            "fullattention" | "mlaattention" | "sinkfullattention"
        )
    }

    /// Validate every block's extra keys so non-text payloads cannot acquire
    /// token-only continuation hashes. Salt is present only at prefix block 0.
    pub(super) fn text_identity(&self) -> Option<CacheIdentity> {
        let extras = self.extra_keys.as_ref()?;
        if extras.len() != self.block_hashes.len() || extras.is_empty() {
            return None;
        }
        let adapter = match &self.lora_name {
            WireField::Null => AdapterIdentity::Base,
            WireField::Value(name) if !name.is_empty() => {
                AdapterIdentity::Lora { name: name.clone() }
            }
            _ => return None,
        };
        let root = matches!(self.parent, WireField::Null);
        let mut salt = None;
        for (index, extra) in extras.iter().enumerate() {
            let keys: &[Value] = match extra {
                Value::Nil => &[],
                Value::Array(keys) if !keys.is_empty() => keys,
                _ => return None,
            };
            let remaining = match &adapter {
                AdapterIdentity::Base => keys,
                AdapterIdentity::Lora { name } => {
                    if keys.first().and_then(Value::as_str) != Some(name) {
                        return None;
                    }
                    &keys[1..]
                }
            };
            match remaining {
                [] => {}
                [value] if root && index == 0 => salt = Some(value.as_str()?.to_owned()),
                _ => return None,
            }
        }
        let identity = CacheIdentity {
            schema_version: 1,
            adapter,
            cache_salt: salt,
        };
        identity.validate().ok()?;
        Some(identity)
    }
}

#[derive(Debug, Clone)]
pub(crate) struct IdentityRemovedEvent {
    pub(super) block_hashes: Vec<EngineBlockHash>,
    pub(super) medium: Option<String>,
}

impl IdentityRemovedEvent {
    pub(super) fn from_fields<'a>(get: impl Fn(&str) -> Option<&'a Value>) -> Option<Self> {
        Some(Self {
            block_hashes: engine_hashes(get("block_hashes")?)?,
            medium: optional_value(get("medium"), string)?,
        })
    }

    pub(super) fn from_values(values: &[Value]) -> Option<Self> {
        Self::from_fields(|name| {
            values.get(match name {
                "block_hashes" => 0,
                "medium" => 1,
                _ => return None,
            })
        })
    }
}
