// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Identity values and lossless engine identifiers. Only the prefix root mixes identity.

use serde::{Deserialize, Serialize};

use crate::error::KvConductorError;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, clap::ValueEnum)]
pub enum IdentityMode {
    #[default]
    Legacy,
    V1,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct IdentityConfig {
    pub mode: IdentityMode,
}

impl IdentityConfig {
    pub fn enabled(self) -> bool {
        self.mode == IdentityMode::V1
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub enum AdapterIdentity {
    Base,
    Lora { name: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CacheIdentity {
    pub schema_version: u32,
    pub adapter: AdapterIdentity,
    pub cache_salt: Option<String>,
}

impl CacheIdentity {
    pub fn validate(&self) -> Result<(), KvConductorError> {
        if self.schema_version != 1 {
            return Err(KvConductorError::InvalidIdentity(
                "unsupported cache_identity schema_version".into(),
            ));
        }
        let valid_string = |value: &str| !value.is_empty() && u32::try_from(value.len()).is_ok();
        if matches!(&self.adapter, AdapterIdentity::Lora { name } if !valid_string(name))
            || self
                .cache_salt
                .as_deref()
                .is_some_and(|salt| !valid_string(salt))
        {
            return Err(KvConductorError::InvalidIdentity(
                "LoRA name and cache_salt must be nonempty encodable strings".into(),
            ));
        }
        Ok(())
    }

    /// Append the validated identity. The protocol schema version is not hashed.
    pub fn encode_into(&self, bytes: &mut Vec<u8>) {
        fn append_string(bytes: &mut Vec<u8>, value: &str) {
            bytes.extend_from_slice(&(value.len() as u32).to_le_bytes());
            bytes.extend_from_slice(value.as_bytes());
        }
        match &self.adapter {
            AdapterIdentity::Base => bytes.push(0),
            AdapterIdentity::Lora { name } => {
                bytes.push(1);
                append_string(bytes, name);
            }
        }
        match &self.cache_salt {
            None => bytes.push(0),
            Some(salt) => {
                bytes.push(1);
                append_string(bytes, salt);
            }
        }
    }
}

/// Complete engine value, never a low-64-bit projection of a longer hash.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum EngineBlockHash {
    U64(u64),
    Bytes(Vec<u8>),
}

impl EngineBlockHash {
    pub fn from_bytes(bytes: &[u8]) -> Self {
        if bytes.len() <= 8 {
            let mut value = [0; 8];
            value[8 - bytes.len()..].copy_from_slice(bytes);
            Self::U64(u64::from_be_bytes(value))
        } else {
            Self::Bytes(bytes.to_vec())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn omitted_and_null_salt_decode_to_the_same_identity() {
        let missing = r#"{"schema_version":1,"adapter":{"kind":"base"}}"#;
        let null = r#"{"schema_version":1,"adapter":{"kind":"base"},"cache_salt":null}"#;
        let identity = serde_json::from_str::<CacheIdentity>(missing).unwrap();
        assert_eq!(identity.cache_salt, None);
        assert_eq!(
            identity,
            serde_json::from_str::<CacheIdentity>(null).unwrap()
        );
    }

    #[test]
    fn identity_rejects_unsupported_values() {
        for invalid in [
            r#"{"schema_version":2,"adapter":{"kind":"base"},"cache_salt":null}"#,
            r#"{"schema_version":1,"adapter":{"kind":"lora","name":""},"cache_salt":null}"#,
            r#"{"schema_version":1,"adapter":{"kind":"base"},"cache_salt":""}"#,
        ] {
            assert!(serde_json::from_str::<CacheIdentity>(invalid)
                .unwrap()
                .validate()
                .is_err());
        }
    }

    #[test]
    fn encoding_preserves_field_boundaries_and_omits_schema() {
        let identity = CacheIdentity {
            schema_version: 1,
            adapter: AdapterIdentity::Lora { name: "a".into() },
            cache_salt: Some("bc".into()),
        };
        let mut bytes = Vec::new();
        identity.encode_into(&mut bytes);
        assert_eq!(bytes, [1, 1, 0, 0, 0, b'a', 1, 2, 0, 0, 0, b'b', b'c']);
        let encoded = rmp_serde::to_vec_named(&identity).unwrap();
        assert_eq!(
            rmp_serde::from_slice::<CacheIdentity>(&encoded).unwrap(),
            identity
        );
    }

    #[test]
    fn complete_hashes_with_equal_low_bits_remain_distinct() {
        assert_eq!(EngineBlockHash::from_bytes(&[1]), EngineBlockHash::U64(1));
        let mut a = vec![0; 32];
        let mut b = a.clone();
        a[0] = 1;
        b[0] = 2;
        assert_ne!(
            EngineBlockHash::from_bytes(&a),
            EngineBlockHash::from_bytes(&b)
        );
    }
}
