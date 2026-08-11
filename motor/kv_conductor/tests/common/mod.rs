// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Shared helpers for integration tests.
//!
//! Note: unit tests inside `src/` cannot reference this directory (separate
//! crates), so `src/protocols.rs` keeps its own copy of `rmpv_to_json`.

/// Convert a MessagePack value into its JSON equivalent for structural
/// comparison with the JSON wire shape.
pub fn rmpv_to_json(v: &rmpv::Value) -> serde_json::Value {
    use serde_json::Value;
    match v {
        rmpv::Value::Nil => Value::Null,
        rmpv::Value::Boolean(b) => Value::Bool(*b),
        rmpv::Value::Integer(i) => {
            if let Some(u) = i.as_u64() {
                Value::from(u)
            } else {
                Value::from(i.as_i64().unwrap_or_default())
            }
        }
        rmpv::Value::F64(f) => Value::from(*f),
        rmpv::Value::F32(f) => Value::from(*f),
        rmpv::Value::String(s) => Value::String(s.as_str().unwrap_or_default().to_string()),
        rmpv::Value::Binary(b) => Value::String(format!("{b:?}")),
        rmpv::Value::Array(a) => Value::Array(a.iter().map(rmpv_to_json).collect()),
        rmpv::Value::Map(m) => {
            let mut map = serde_json::Map::new();
            for (k, val) in m {
                let key = match k {
                    rmpv::Value::String(s) => s.as_str().unwrap_or_default().to_string(),
                    other => format!("{other:?}"),
                };
                map.insert(key, rmpv_to_json(val));
            }
            Value::Object(map)
        }
        rmpv::Value::Ext(..) => Value::Null,
    }
}
