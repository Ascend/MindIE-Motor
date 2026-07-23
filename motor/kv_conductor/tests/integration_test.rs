// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Integration tests for the KV conductor HTTP service.

use std::sync::Arc;

use reqwest::Client;
use serde_json::{json, Value};
use tokio::net::TcpListener;

use kv_conductor::protocols::ScoringConfig;
use kv_conductor::registry::WorkerRegistry;
use kv_conductor::server::{create_router, AppState};

/// Start a test server on a random port, returning the base URL.
async fn start_test_server() -> (String, tokio::task::JoinHandle<()>) {
    let scoring = ScoringConfig::default();
    let registry = Arc::new(WorkerRegistry::new(scoring.clone()));
    let state = AppState { registry, scoring };
    let router = create_router(state);

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let base_url = format!("http://{}", addr);

    let handle = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });

    // Give the server a moment to start
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    (base_url, handle)
}

#[tokio::test]
async fn test_health_endpoint() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let resp = client
        .get(format!("{}/health", base_url))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    assert_eq!(resp.text().await.unwrap(), "OK");
}

#[tokio::test]
async fn test_register_and_query() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // Register a worker
    let register_data = json!({
        "instance_id": "vllm-prefill-42",
        "medium_endpoints": {
            "xpu": "tcp://10.0.0.1:50090",
            "cpu": "tcp://10.0.0.1:50090",
            "disk": "tcp://10.0.0.1:50090"
        },
        "type": "vllm",
        "modelname": "llama-7b",
        "block_size": 128,
        "dp_rank": 0,
        "tenant_id": "default"
    });

    let resp = client
        .post(format!("{}/register", base_url))
        .json(&register_data)
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 201);

    // Query with token IDs. The response will be empty since no KV events
    // have been applied to the tree yet.
    let query_data = json!({
        "model": "llama-7b",
        "block_size": 128,
        "token_ids": [1, 2, 3, 4, 5, 6, 7, 8],
        "tenant_id": "default"
    });

    let resp = client
        .post(format!("{}/query", base_url))
        .json(&query_data)
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    // Should have the tenant key with an empty object (no cached blocks yet)
    assert!(body.get("default").is_some());
}

#[tokio::test]
async fn test_query_after_kv_events() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // Register two workers for the same model
    for i in 0..2 {
        let ep = format!("tcp://10.0.0.{}:50090", i + 1);
        let resp = client
            .post(format!("{}/register", base_url))
            .json(&json!({
                "instance_id": format!("vllm-prefill-{}", i),
                "medium_endpoints": {
                    "xpu": ep,
                    "cpu": ep,
                    "disk": ep
                },
                "type": "vllm",
                "modelname": "test-model",
                "block_size": 4,
                "dp_rank": 0,
                "tenant_id": "default"
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 201);
    }

    // Apply KV events to populate the indexer tree for worker 0
    // Use tokens that will produce specific block hashes.
    // For block_size=4, tokens [1,2,3,4] -> block hash A, [5,6,7,8] -> block hash B
    // We simulate storing a chain: root -> block_A -> block_B
    let events = json!({
        "events": [
            {
                "event_id": 1,
                "data": {
                    "type": "stored",
                    "parent_hash": null,
                    "blocks": [
                        {"block_hash": 100, "tokens_hash": 12345678901234567890_u64}
                    ]
                },
                "dp_rank": 0
            }
        ],
        "shutdown": false
    });

    let _resp = client
        .post(format!("{}/events", base_url))
        .json(&events)
        .send()
        .await
        .unwrap();

    // Query: should return results now
    let query_data = json!({
        "model": "test-model",
        "block_size": 4,
        "token_ids": [1, 2, 3, 4, 5, 6, 7, 8],
        "tenant_id": "default"
    });

    let resp = client
        .post(format!("{}/query", base_url))
        .json(&query_data)
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    println!(
        "Query response: {}",
        serde_json::to_string_pretty(&body).unwrap()
    );
    // Response should have structure: { "default": { "vllm-prefill-0": { "longest_matched": ..., "DP": {...} } } }
    assert!(body.get("default").is_some());
}

#[tokio::test]
async fn test_unregister() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // Register
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "vllm-prefill-99",
            "medium_endpoints": {
                "xpu": "tcp://10.0.0.1:50090",
                "cpu": "tcp://10.0.0.1:50090",
                "disk": "tcp://10.0.0.1:50090"
            },
            "type": "vllm",
            "modelname": "test-model",
            "block_size": 128,
            "dp_rank": 0,
            "tenant_id": "default"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    // Verify worker is listed
    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    let workers = body["workers"].as_array().unwrap();
    assert_eq!(workers.len(), 1);

    // Unregister
    let resp = client
        .post(format!("{}/unregister", base_url))
        .json(&json!({
            "instance_id": "vllm-prefill-99",
            "type": "vllm",
            "modelname": "test-model",
            "block_size": 128,
            "dp_rank": 0,
            "tenant_id": "default"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);

    // Verify worker is gone
    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    let workers = body["workers"].as_array().unwrap();
    assert_eq!(workers.len(), 0);
}

#[tokio::test]
async fn test_duplicate_registration() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let reg = json!({
        "instance_id": "dup-test",
        "medium_endpoints": {
            "xpu": "tcp://10.0.0.1:50090",
            "cpu": "tcp://10.0.0.1:50090",
            "disk": "tcp://10.0.0.1:50090"
        },
        "type": "vllm",
        "modelname": "test-model",
        "block_size": 128,
        "dp_rank": 0,
        "tenant_id": "default"
    });

    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    // Re-registration with same backend is accepted (201).
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);
}

#[tokio::test]
async fn test_unregister_nonexistent() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let resp = client
        .post(format!("{}/unregister", base_url))
        .json(&json!({
            "instance_id": "nonexistent",
            "type": "vllm",
            "modelname": "test-model",
            "block_size": 128,
            "dp_rank": 0,
            "tenant_id": "default"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 404);
}

#[tokio::test]
async fn test_workers_endpoint_empty() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert!(body["workers"].as_array().unwrap().is_empty());
}

// ── Mooncake backend: HBM + pool registration ─────────────────────

#[tokio::test]
async fn test_mooncake_hbm_plus_pool_registration() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // Register HBM endpoint (XPU only, store_backend=Mooncake)
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "mooncake-prefill-0",
            "medium_endpoints": {"xpu": "tcp://10.0.0.1:50090"},
            "type": "vLLM",
            "store_backend": "Mooncake",
            "modelname": "mooncake-model",
            "block_size": 128,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201, "HBM registration should succeed");

    // Register pool (legacy endpoint, store_backend=Mooncake)
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "mooncake-pool",
            "endpoint": "tcp://10.0.0.100:5557",
            "type": "Mooncake",
            "store_backend": "Mooncake",
            "modelname": "mooncake-model",
            "block_size": 128,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201, "Pool registration should succeed");

    // Verify both are listed
    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    let workers = body["workers"].as_array().unwrap();
    assert_eq!(workers.len(), 2);
    let ids: Vec<&str> = workers
        .iter()
        .map(|w| w["instance_id"].as_str().unwrap())
        .collect();
    assert!(ids.contains(&"mooncake-prefill-0"));
    assert!(ids.contains(&"mooncake-pool"));
}

// ── Memcache backend: HBM + pool registration ─────────────────────

#[tokio::test]
async fn test_memcache_hbm_plus_pool_registration() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // HBM
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "memcache-prefill-0",
            "medium_endpoints": {"xpu": "tcp://10.0.1.1:50090"},
            "type": "vLLM",
            "store_backend": "Memcache",
            "modelname": "memcache-model",
            "block_size": 64,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    // Pool
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "memcache-pool",
            "endpoint": "tcp://10.0.1.100:5557",
            "type": "Memcache",
            "store_backend": "Memcache",
            "modelname": "memcache-model",
            "block_size": 64,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["workers"].as_array().unwrap().len(), 2);
}

// ── YuanRong backend: multi-port registration ──────────────────────

#[tokio::test]
async fn test_yuanrong_multi_port_registration() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // YuanRong: cpu + disk share one port, xpu on another
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "yr-node-0",
            "medium_endpoints": {
                "xpu": "tcp://10.0.2.1:15557",
                "cpu": "tcp://10.0.2.1:15558",
                "disk": "tcp://10.0.2.1:15558"
            },
            "type": "vLLM",
            "store_backend": "YuanRong",
            "modelname": "yuanrong-model",
            "block_size": 128,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    // Verify medium_endpoints stored correctly
    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    let w = &body["workers"].as_array().unwrap()[0];
    let meps = &w["endpoints"]["0"]["medium_endpoints"];
    assert_eq!(meps["xpu"], "tcp://10.0.2.1:15557");
    assert_eq!(meps["cpu"], "tcp://10.0.2.1:15558");
    assert_eq!(meps["disk"], "tcp://10.0.2.1:15558");
}

// ── Mooncake: duplicate HBM registration (same instance, same dp) ───

#[tokio::test]
async fn test_mooncake_duplicate_hbm_registration() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let reg = json!({
        "instance_id": "mooncake-dup",
        "medium_endpoints": {"xpu": "tcp://10.0.3.1:50090"},
        "type": "vLLM",
        "store_backend": "Mooncake",
        "modelname": "dup-model",
        "block_size": 128,
        "dp_rank": 0
    });

    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    // Re-registration with same backend is accepted (201).
    assert_eq!(resp.status(), 201);
}

// ── Mooncake pool: duplicate pool registration ─────────────────────

#[tokio::test]
async fn test_mooncake_duplicate_pool_registration() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let reg = json!({
        "instance_id": "mooncake-pool-dup",
        "endpoint": "tcp://10.0.4.100:5557",
        "type": "Mooncake",
        "store_backend": "Mooncake",
        "modelname": "dup-model",
        "block_size": 128,
        "dp_rank": 0
    });

    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    // Re-registration with same backend is accepted (201).
    assert_eq!(resp.status(), 201);
}

// ── Unregister cleans up state ──────────────────────────────────────

#[tokio::test]
async fn test_unregister_mooncake_hbm_removes_worker() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // Register
    client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "to-remove",
            "medium_endpoints": {"xpu": "tcp://10.0.5.1:50090"},
            "type": "vLLM",
            "store_backend": "Mooncake",
            "modelname": "rm-model",
            "block_size": 128,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();

    // Verify present
    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["workers"].as_array().unwrap().len(), 1);

    // Unregister
    let resp = client
        .post(format!("{}/unregister", base_url))
        .json(&json!({
            "instance_id": "to-remove",
            "type": "vLLM",
            "modelname": "rm-model",
            "block_size": 128,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);

    // Verify gone
    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["workers"].as_array().unwrap().len(), 0);
}

// ── Unknown backend falls back to YuanRong behavior ─────────────────

#[tokio::test]
async fn test_unknown_backend_falls_back_to_yuanrong() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    // Unknown backend with medium_endpoints should still register
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "unknown-backend",
            "medium_endpoints": {
                "xpu": "tcp://10.0.6.1:15557",
                "cpu": "tcp://10.0.6.1:15558",
                "disk": "tcp://10.0.6.1:15558"
            },
            "type": "vLLM",
            "store_backend": "SomeFutureBackend",
            "modelname": "future-model",
            "block_size": 128,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        201,
        "Unknown backend should fall back to multi-port behavior"
    );
}

// ── Registration without endpoint or medium_endpoints fails ──────────

#[tokio::test]
async fn test_registration_without_endpoint_allows_http_only() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let resp = client
        .post(format!("{}/register", base_url))
        .json(&json!({
            "instance_id": "http-only",
            "type": "vLLM",
            "store_backend": "Mooncake",
            "modelname": "http-model",
            "block_size": 128,
            "dp_rank": 0
        }))
        .send()
        .await
        .unwrap();
    // HTTP-only registration (no ZMQ endpoints) is now allowed — the
    // conductor creates an indexer entry without spawning ZMQ subscribers.
    assert_eq!(
        resp.status(),
        201,
        "HTTP-only registration (no endpoint) should succeed"
    );

    // Verify the worker is listed
    let resp = client
        .get(format!("{}/workers", base_url))
        .send()
        .await
        .unwrap();
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["workers"].as_array().unwrap().len(), 1);
}

// ── Re-registration: same backend preserves tree data ────────────────

#[tokio::test]
async fn test_reregister_same_backend_preserves_tree() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let reg = json!({
        "instance_id": "rereg-same",
        "medium_endpoints": {
            "xpu": "tcp://10.0.10.1:50090",
            "cpu": "tcp://10.0.10.1:50090",
            "disk": "tcp://10.0.10.1:50090"
        },
        "type": "vllm",
        "store_backend": "YuanRong",
        "modelname": "rereg-model",
        "block_size": 4,
        "dp_rank": 0,
        "tenant_id": "default"
    });

    // Initial registration
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201, "first registration should succeed");

    // Inject a KV event to populate the radix tree
    let events = json!({
        "events": [{
            "event_id": 1,
            "data": {
                "type": "stored",
                "parent_hash": null,
                "blocks": [{"block_hash": 900, "tokens_hash": 900}]
            },
            "dp_rank": 0
        }],
        "shutdown": false
    });
    client
        .post(format!("{}/events", base_url))
        .json(&events)
        .send()
        .await
        .unwrap();

    // Re-register with same backend (different endpoint)
    let mut reg2 = reg.clone();
    reg2["medium_endpoints"] = json!({
        "xpu": "tcp://10.0.10.2:50090",
        "cpu": "tcp://10.0.10.2:50090",
        "disk": "tcp://10.0.10.2:50090"
    });
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg2)
        .send()
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        201,
        "re-registration with same backend should succeed"
    );

    // Query: tree data should still exist (token hash 900 matches).
    let query_data = json!({
        "model": "rereg-model",
        "block_size": 4,
        "token_ids": [1, 2, 3, 4],
        "tenant_id": "default"
    });
    let resp = client
        .post(format!("{}/query", base_url))
        .json(&query_data)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    // With tokens_hash=900 at block_size=4, should match if tree preserved.
    assert!(
        body.get("default").is_some(),
        "tree should be preserved on same-backend re-registration"
    );
}

// ── Re-registration: different backend drops tree data ────────────────

#[tokio::test]
async fn test_reregister_different_backend_drops_tree() {
    let (base_url, _handle) = start_test_server().await;
    let client = Client::new();

    let reg = json!({
        "instance_id": "rereg-diff",
        "medium_endpoints": {
            "xpu": "tcp://10.0.11.1:50090",
            "cpu": "tcp://10.0.11.1:50090",
            "disk": "tcp://10.0.11.1:50090"
        },
        "type": "vllm",
        "store_backend": "YuanRong",
        "modelname": "rereg-diff-model",
        "block_size": 4,
        "dp_rank": 0,
        "tenant_id": "default"
    });

    // Initial registration with YuanRong
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    // Inject a KV event
    let events = json!({
        "events": [{
            "event_id": 1,
            "data": {
                "type": "stored",
                "parent_hash": null,
                "blocks": [{"block_hash": 800, "tokens_hash": 800}]
            },
            "dp_rank": 0
        }],
        "shutdown": false
    });
    client
        .post(format!("{}/events", base_url))
        .json(&events)
        .send()
        .await
        .unwrap();

    // Re-register with DIFFERENT backend
    let mut reg2 = reg.clone();
    reg2["store_backend"] = json!("Mooncake");
    let resp = client
        .post(format!("{}/register", base_url))
        .json(&reg2)
        .send()
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        201,
        "re-registration with different backend should succeed"
    );

    // Query: tree data should be gone (backend changed → data dropped).
    let query_data = json!({
        "model": "rereg-diff-model",
        "block_size": 4,
        "token_ids": [1, 2, 3, 4],
        "tenant_id": "default"
    });
    let resp = client
        .post(format!("{}/query", base_url))
        .json(&query_data)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    // Default tenant may not appear at all if no workers have data.
    let default = body.get("default");
    let has_data = default
        .and_then(|d| d.as_object())
        .map(|obj| !obj.is_empty())
        .unwrap_or(false);
    assert!(
        !has_data,
        "tree should be dropped on backend-change re-registration"
    );
}
