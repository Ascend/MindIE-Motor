// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Local vLLM ZMQ event ingestion followed by public HTTP identity queries.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use serde_json::{json, Value};
use tokio::net::TcpListener;

use kv_conductor::identity::{IdentityConfig, IdentityMode};
use kv_conductor::indexer::CacheMaintenanceConfig;
use kv_conductor::registry::WorkerRegistry;
use kv_conductor::server::{create_router, AppState};

fn publish(publisher: &zmq::Socket, event: &Value) {
    let payload = rmp_serde::to_vec(&(1.0_f64, vec![event], 0_u32)).unwrap();
    publisher
        .send_multipart(
            [
                b"kv-events".as_slice(),
                &0_u64.to_be_bytes(),
                payload.as_slice(),
            ],
            0,
        )
        .unwrap();
}

fn identity(name: &str, salt: &str) -> Value {
    json!({"schema_version": 1, "adapter": {"kind": "lora", "name": name}, "cache_salt": salt})
}

async fn coverage(client: &Client, base_url: &str, identity: &Value) -> u64 {
    let response = client
        .post(format!("{base_url}/query"))
        .json(&json!({
            "model": "model", "tenant_id": "default", "block_size": 2,
            "token_ids": [1, 2, 3, 4], "cache_identity": identity,
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["X-KV-Identity-Protocol"], "1");
    let body: Value = response.json().await.unwrap();
    body["default"]["engine"]["DP"]["0"]["npu_blocks"]
        .as_u64()
        .unwrap_or(0)
}

async fn wait_coverage(
    client: &Client,
    base_url: &str,
    identity: &Value,
    expected: u64,
    initial_publication: Option<(&zmq::Socket, &Value)>,
) {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            // PUB may drop the first messages before the subscriber connects.
            // Stop retrying once HTTP observes the root; later events are sent once.
            if let Some((publisher, event)) = initial_publication {
                publish(publisher, event);
            }
            if coverage(client, base_url, identity).await == expected {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("ZMQ events must reach the HTTP query within five seconds");
}

#[tokio::test]
async fn zmq_root_continuation_and_removal_preserve_http_identity_isolation() {
    let registry = Arc::new(WorkerRegistry::with_identity_config(
        CacheMaintenanceConfig::default(),
        IdentityConfig {
            mode: IdentityMode::V1,
        },
    ));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base_url = format!("http://{}", listener.local_addr().unwrap());
    let router = create_router(AppState { registry });
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let client = Client::new();

    let context = zmq::Context::new();
    let publisher = context.socket(zmq::PUB).unwrap();
    publisher.set_linger(0).unwrap();
    publisher.set_sndtimeo(1000).unwrap();
    publisher.bind("tcp://127.0.0.1:*").unwrap();
    let endpoint = publisher.get_last_endpoint().unwrap().unwrap();
    let registration = json!({
        "instance_id": "engine", "modelname": "model", "type": "vllm",
        "block_size": 2, "dp_rank": 0, "tenant_id": "default",
        "medium_endpoints": {"npu": endpoint},
    });
    let response = client
        .post(format!("{base_url}/register"))
        .json(&registration)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 201);

    let root = json!({
        "type": "BlockStored", "block_hashes": [100], "parent_block_hash": null,
        "token_ids": [1, 2], "block_size": 2, "medium": "GPU", "lora_name": "adapter-A",
        "extra_keys": [["adapter-A", "salt-A"]], "group_idx": 0, "kv_cache_spec_kind": "FullAttention",
    });
    let matched_identity = identity("adapter-A", "salt-A");
    wait_coverage(
        &client,
        &base_url,
        &matched_identity,
        1,
        Some((&publisher, &root)),
    )
    .await;

    // The continuation carries its parent and LoRA evidence, but no root salt.
    publish(
        &publisher,
        &json!({
            "type": "BlockStored", "block_hashes": [101], "parent_block_hash": 100,
            "token_ids": [3, 4], "block_size": 2, "medium": "GPU", "lora_name": "adapter-A",
            "extra_keys": [["adapter-A"]], "group_idx": 0, "kv_cache_spec_kind": "FullAttention",
        }),
    );
    wait_coverage(&client, &base_url, &matched_identity, 2, None).await;
    for different_identity in [
        identity("adapter-B", "salt-A"),
        identity("adapter-A", "salt-B"),
    ] {
        assert_eq!(coverage(&client, &base_url, &different_identity).await, 0);
    }

    publish(
        &publisher,
        &json!({
            "type": "BlockRemoved", "block_hashes": [100, 101], "medium": "GPU", "group_idx": 0,
        }),
    );
    wait_coverage(&client, &base_url, &matched_identity, 0, None).await;

    let response = client
        .post(format!("{base_url}/unregister"))
        .json(&registration)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    server.abort();
}
