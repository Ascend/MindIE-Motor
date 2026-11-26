// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Identity protocol behavior through the public HTTP routes.

use std::sync::Arc;

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use axum::Router;
use serde_json::{json, Value};
use tower::ServiceExt;

use kv_conductor::hashing::compute_block_hash_for_seq;
use kv_conductor::identity::{
    AdapterIdentity, CacheIdentity, EngineBlockHash, IdentityConfig, IdentityMode,
};
use kv_conductor::indexer::CacheMaintenanceConfig;
use kv_conductor::protocols::{StorageMedium, WorkerKey};
use kv_conductor::registry::WorkerRegistry;
use kv_conductor::server::{create_router, AppState};

mod common;

fn test_app(mode: IdentityMode) -> (Router, Arc<WorkerRegistry>) {
    let registry = Arc::new(WorkerRegistry::with_identity_config(
        CacheMaintenanceConfig::default(),
        IdentityConfig { mode },
    ));
    (
        create_router(AppState {
            registry: Arc::clone(&registry),
        }),
        registry,
    )
}

async fn post(app: &Router, path: &str, value: &Value, msgpack: bool) -> (StatusCode, bool, Value) {
    let body = if msgpack {
        rmp_serde::to_vec_named(value).unwrap()
    } else {
        serde_json::to_vec(value).unwrap()
    };
    let response = app
        .clone()
        .oneshot(
            Request::post(path)
                .header(
                    "Content-Type",
                    if msgpack {
                        "application/msgpack"
                    } else {
                        "application/json"
                    },
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let confirmed = response
        .headers()
        .get("X-KV-Identity-Protocol")
        .is_some_and(|value| value == "1");
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    let value = if msgpack {
        common::rmpv_to_json(&rmpv::decode::read_value(&mut std::io::Cursor::new(bytes)).unwrap())
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, confirmed, value)
}

async fn register(app: &Router, instance_id: &str, block_size: u32) -> StatusCode {
    post(
        app,
        "/register",
        &json!({
            "instance_id": instance_id, "modelname": "model", "type": "vllm",
            "block_size": block_size, "dp_rank": 0, "tenant_id": "default",
        }),
        false,
    )
    .await
    .0
}

fn identities() -> Vec<CacheIdentity> {
    [
        AdapterIdentity::Base,
        AdapterIdentity::Lora { name: "A".into() },
        AdapterIdentity::Lora { name: "B".into() },
    ]
    .into_iter()
    .flat_map(|adapter| {
        [None, Some("salt-A".into()), Some("salt-B".into())]
            .into_iter()
            .map(move |cache_salt| CacheIdentity {
                schema_version: 1,
                adapter: adapter.clone(),
                cache_salt,
            })
    })
    .collect()
}

fn query_body(path: &str, identity: &CacheIdentity) -> Value {
    let mut body = json!({"model": "model", "tenant_id": "default", "block_size": 4, "cache_identity": identity});
    if path == "/query" {
        body["token_ids"] = json!([1, 2, 3, 4, 5, 6, 7, 8]);
    } else {
        body["block_hashes"] = json!(compute_block_hash_for_seq(&[1, 2, 3, 4, 5, 6, 7, 8], 4)
            .into_iter()
            .map(|hash| hash.0)
            .collect::<Vec<_>>());
    }
    body
}

#[tokio::test]
async fn both_query_routes_isolate_nine_identities_in_json_and_msgpack() {
    let (app, registry) = test_app(IdentityMode::V1);
    let identities = identities();
    let tokens = [1, 2, 3, 4, 5, 6, 7, 8];
    let hashes: Vec<_> = compute_block_hash_for_seq(&tokens, 4)
        .into_iter()
        .map(|hash| hash.0)
        .collect();
    for (index, identity) in identities.iter().enumerate() {
        let instance_id = format!("worker-{index}");
        assert_eq!(register(&app, &instance_id, 4).await, StatusCode::CREATED);
        let worker = WorkerKey {
            instance_id: instance_id.clone(),
            backend_id: instance_id,
            dp_rank: 0,
            medium: StorageMedium::Npu,
        };
        registry
            .indexer()
            .get("model", "default")
            .unwrap()
            .apply_identity_store(
                &worker,
                &[
                    EngineBlockHash::U64(100 + index as u64 * 2),
                    EngineBlockHash::U64(101 + index as u64 * 2),
                ],
                None,
                &hashes,
                Some(identity),
                false,
            )
            .unwrap();
    }
    for path in ["/query", "/query_by_hash"] {
        for msgpack in [false, true] {
            for (index, identity) in identities.iter().enumerate() {
                let mut body = query_body(path, identity);
                let (status, confirmed, response) = post(&app, path, &body, msgpack).await;
                assert_eq!(status, StatusCode::OK);
                assert!(confirmed);
                if identity.cache_salt.is_none() {
                    body["cache_identity"]
                        .as_object_mut()
                        .unwrap()
                        .remove("cache_salt");
                    assert_eq!(
                        post(&app, path, &body, msgpack).await,
                        (status, confirmed, response.clone())
                    );
                }
                for other in 0..identities.len() {
                    let worker = &response["default"][format!("worker-{other}")];
                    if index == other {
                        assert_eq!(worker["DP"]["0"]["npu_blocks"], 2);
                        assert_eq!(
                            worker["longest_matched"],
                            if path == "/query" { 8 } else { 2 }
                        );
                    } else {
                        assert!(
                            worker.is_null(),
                            "query identity {index} matched worker {other}"
                        );
                    }
                }
            }
        }
    }
}

#[tokio::test]
async fn v1_acknowledges_zero_hits_but_not_invalid_identity() {
    let (app, _) = test_app(IdentityMode::V1);
    assert_eq!(register(&app, "worker", 4).await, StatusCode::CREATED);
    for path in ["/query", "/query_by_hash"] {
        for msgpack in [false, true] {
            let body = query_body(path, &identities()[0]);
            let (status, confirmed, response) = post(&app, path, &body, msgpack).await;
            assert_eq!(status, StatusCode::OK);
            assert!(confirmed);
            assert_eq!(response, json!({"default": {}}));
            for mutation in ["missing", "version", "salt", "malformed"] {
                let mut invalid = body.clone();
                match mutation {
                    "missing" => {
                        invalid.as_object_mut().unwrap().remove("cache_identity");
                    }
                    "version" => invalid["cache_identity"]["schema_version"] = json!(2),
                    "salt" => invalid["cache_identity"]["cache_salt"] = json!(""),
                    "malformed" => invalid["cache_identity"] = json!("invalid"),
                    _ => unreachable!(),
                }
                let (status, confirmed, _) = post(&app, path, &invalid, msgpack).await;
                assert_eq!(
                    status,
                    StatusCode::BAD_REQUEST,
                    "{path} accepted {mutation}"
                );
                assert!(!confirmed);
            }
            let mut missing_model = body;
            missing_model["model"] = json!("unknown");
            let (status, confirmed, _) = post(&app, path, &missing_model, msgpack).await;
            assert_eq!(status, StatusCode::NOT_FOUND);
            assert!(!confirmed);
        }
    }
    let (status, confirmed, _) = post(
        &app,
        "/events",
        &json!({"instance_id": "worker", "events": []}),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(!confirmed);

    let response = app
        .oneshot(Request::get("/metrics").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    let metrics = std::str::from_utf8(&bytes).unwrap();
    for endpoint in ["query", "query_by_hash"] {
        // Each encoding made one empty query, four bad requests, and one query for a missing model.
        for (result, count) in [("empty", 2), ("bad_request", 8), ("no_indexer", 2)] {
            let sample = format!(
                r#"kv_conductor_queries_total{{endpoint="{endpoint}",result="{result}"}} {count}"#
            );
            assert!(metrics.lines().any(|line| line == sample), "{sample}");
        }
        for (reason, count) in [("bad_request", 8), ("no_indexer", 2), ("internal", 0)] {
            let sample = format!(
                r#"kv_conductor_query_errors_total{{endpoint="{endpoint}",reason="{reason}"}} {count}"#
            );
            assert!(metrics.lines().any(|line| line == sample), "{sample}");
        }
        let sample =
            format!(r#"kv_conductor_query_duration_seconds_count{{endpoint="{endpoint}"}} 12"#);
        assert!(metrics.lines().any(|line| line == sample), "{sample}");
    }
    assert!(metrics.lines().any(|line| {
        line == r#"kv_conductor_event_batches_total{source="http",status="error"} 1"#
    }));
}

#[tokio::test]
async fn legacy_queries_keep_existing_body_without_identity_confirmation() {
    let (app, _) = test_app(IdentityMode::Legacy);
    assert_eq!(register(&app, "worker", 4).await, StatusCode::CREATED);
    for path in ["/query", "/query_by_hash"] {
        for msgpack in [false, true] {
            let mut body = query_body(path, &identities()[0]);
            body.as_object_mut().unwrap().remove("cache_identity");
            let (status, confirmed, response) = post(&app, path, &body, msgpack).await;
            assert_eq!(status, StatusCode::OK);
            assert!(!confirmed);
            assert_eq!(response, json!({"default": {}}));
        }
    }
}
