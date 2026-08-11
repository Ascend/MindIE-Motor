// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Axum HTTP server for the KV conductor.
//!
//! Provides the following endpoints:
//! - `POST /register`       — Register a worker instance
//! - `POST /unregister`     — Unregister a worker instance
//! - `POST /query`          — Query KV cache matched blocks by token IDs
//! - `POST /query_by_hash`  — Query KV cache matched blocks by pre-computed hashes
//! - `POST /events`         — Ingest KV cache events from workers
//! - `GET /health`          — Health check
//! - `GET /workers`         — List registered workers (debug)

use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::State,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use tower_http::cors::{Any, CorsLayer};
use tower_http::trace::TraceLayer;

/// Maximum request body size (64 MB). Large queries (402400+ token IDs)
/// exceed axum's default 2 MB limit.
const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

use crate::error::KvConductorError;
use crate::protocols::*;
use crate::registry::WorkerRegistry;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub registry: Arc<WorkerRegistry>,
}

/// Create the axum Router with all endpoints.
pub fn create_router(state: AppState) -> Router {
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let mut router = Router::new()
        .route("/register", post(register_handler))
        .route("/unregister", post(unregister_handler))
        .route("/query", post(query_handler))
        .route("/query_by_hash", post(query_by_hash_handler))
        .route("/events", post(events_handler))
        .route("/health", get(health_handler))
        .route("/workers", get(workers_handler));

    // Raise the global body limit — DeepSeek V4 queries carry 400K+ token
    // IDs (~2.4 MB JSON body), beyond axum's 2 MB default.
    router = router.layer(axum::extract::DefaultBodyLimit::max(MAX_BODY_BYTES));

    router
        .layer(TraceLayer::new_for_http())
        .layer(cors)
        .with_state(state)
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/// POST /register
async fn register_handler(
    State(state): State<AppState>,
    Json(req): Json<RegisterRequest>,
) -> (StatusCode, Json<serde_json::Value>) {
    tracing::info!(
        instance_id = %req.instance_id,
        dp_rank = req.dp_rank,
        model = %req.modelname,
        "register request"
    );

    match state.registry.register(&req).await {
        Ok(()) => (
            StatusCode::CREATED,
            Json(serde_json::json!({"status": "ok"})),
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": e.to_string()})),
        ),
    }
}

/// POST /unregister
async fn unregister_handler(
    State(state): State<AppState>,
    Json(req): Json<UnregisterRequest>,
) -> (StatusCode, Json<serde_json::Value>) {
    tracing::info!(
        instance_id = %req.instance_id,
        dp_rank = req.dp_rank,
        "unregister request"
    );

    match state.registry.unregister(&req).await {
        Ok(()) => (StatusCode::OK, Json(serde_json::json!({"status": "ok"}))),
        Err(KvConductorError::InstanceNotFound { instance_id }) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({"error": format!("instance {} not found", instance_id)})),
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": e.to_string()})),
        ),
    }
}

/// Parse the query request body into `T`, honoring the `Content-Type` header.
///
/// MessagePack bodies (`application/msgpack`) are decoded via `rmp_serde`;
/// everything else is treated as JSON (the historical default). Callers must
/// answer in the same encoding — sniff `is_msgpack_content_type` once before
/// calling and reuse the flag for the response.
fn parse_query_body<T: serde::de::DeserializeOwned>(
    headers: &HeaderMap,
    body: &Bytes,
) -> Result<T, String> {
    if is_msgpack_content_type(headers) {
        rmp_serde::from_slice::<T>(body).map_err(|e| format!("invalid msgpack request body: {e}"))
    } else {
        serde_json::from_slice::<T>(body).map_err(|e| format!("invalid JSON request body: {e}"))
    }
}

/// Render the query error/empty-result responses in the request's encoding.
fn query_error_response(status: StatusCode, message: &str, msgpack: bool) -> Response {
    if msgpack {
        let mut buf = Vec::with_capacity(32 + message.len());
        encode_error_msgpack(message, &mut buf);
        (status, [(header::CONTENT_TYPE, "application/msgpack")], buf).into_response()
    } else {
        (status, Json(serde_json::json!({ "error": message }))).into_response()
    }
}

fn empty_tenant_response(tenant_id: &str, msgpack: bool) -> Response {
    if msgpack {
        let mut buf = Vec::with_capacity(16 + tenant_id.len());
        encode_empty_tenant_msgpack(tenant_id, &mut buf);
        (
            StatusCode::OK,
            [(header::CONTENT_TYPE, "application/msgpack")],
            buf,
        )
            .into_response()
    } else {
        (StatusCode::OK, Json(serde_json::json!({ tenant_id: {} }))).into_response()
    }
}

fn ok_query_response(response: QueryResponse, msgpack: bool) -> Response {
    if msgpack {
        let mut buf = Vec::with_capacity(256);
        encode_query_response_msgpack(&response, &mut buf);
        (
            StatusCode::OK,
            [(header::CONTENT_TYPE, "application/msgpack")],
            buf,
        )
            .into_response()
    } else {
        (
            StatusCode::OK,
            Json(serde_json::to_value(response).unwrap_or_default()),
        )
            .into_response()
    }
}

/// POST /query
///
/// Request body: `{ "model": "...", "block_size": 128, "token_ids": [...], "tenant_id": "default" }`
///
/// Response: `{ "<tenant_id>": { "<instance_id>": { "longest_matched": N,
/// "DP": { "<rank>": { "matched_tokens": N, "npu_blocks": N,
/// "cpu_blocks": N, "disk_blocks": N } } } } }`
///
/// Both JSON (default) and MessagePack (`Content-Type: application/msgpack`)
/// encodings are accepted; the response is returned in the request's encoding.
async fn query_handler(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response {
    let msgpack = is_msgpack_content_type(&headers);
    let req = match parse_query_body::<QueryRequest>(&headers, &body) {
        Ok(parsed) => parsed,
        Err(message) => {
            return query_error_response(StatusCode::BAD_REQUEST, &message, msgpack);
        }
    };

    tracing::debug!(
        model = %req.model,
        tenant = %req.tenant_id,
        num_tokens = req.token_ids.len(),
        msgpack,
        "query request"
    );

    match state.registry.query(&req).await {
        Ok(response) => ok_query_response(response, msgpack),
        Err(KvConductorError::NoIndexer {
            model_name,
            tenant_id,
        }) => query_error_response(
            StatusCode::NOT_FOUND,
            &format!("no indexer for model={model_name} tenant={tenant_id}"),
            msgpack,
        ),
        Err(KvConductorError::NoWorkers {
            model_name: _,
            tenant_id,
        }) => {
            // Return empty response structure matching expected format
            empty_tenant_response(&tenant_id, msgpack)
        }
        Err(e) => query_error_response(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string(), msgpack),
    }
}

/// POST /query_by_hash
///
/// Same semantics as `/query` but accepts pre-computed block hashes instead
/// of raw token IDs, avoiding redundant XXH3 computation. Supports the same
/// JSON / MessagePack content negotiation as `/query`.
async fn query_by_hash_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let msgpack = is_msgpack_content_type(&headers);
    let req = match parse_query_body::<QueryByHashRequest>(&headers, &body) {
        Ok(parsed) => parsed,
        Err(message) => {
            return query_error_response(StatusCode::BAD_REQUEST, &message, msgpack);
        }
    };

    tracing::debug!(
        model = %req.model,
        tenant = %req.tenant_id,
        num_hashes = req.block_hashes.len(),
        msgpack,
        "query_by_hash request"
    );

    match state.registry.query_by_hash(&req).await {
        Ok(response) => ok_query_response(response, msgpack),
        Err(KvConductorError::NoIndexer {
            model_name,
            tenant_id,
        }) => query_error_response(
            StatusCode::NOT_FOUND,
            &format!("no indexer for model={model_name} tenant={tenant_id}"),
            msgpack,
        ),
        Err(KvConductorError::NoWorkers {
            model_name: _,
            tenant_id,
        }) => {
            // Return empty response structure matching expected format
            empty_tenant_response(&tenant_id, msgpack)
        }
        Err(e) => query_error_response(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string(), msgpack),
    }
}

/// POST /events
///
/// Ingest a batch of KV cache events from a worker.
async fn events_handler(
    State(state): State<AppState>,
    Json(mut batch): Json<KvEventBatch>,
) -> (StatusCode, Json<serde_json::Value>) {
    tracing::debug!(
        instance_id = %batch.instance_id,
        num_events = batch.events.len(),
        shutdown = batch.shutdown,
        "events request"
    );

    if batch.events.is_empty() {
        return (
            StatusCode::OK,
            Json(serde_json::json!({"status": "ok", "events_applied": 0})),
        );
    }

    // Process events grouped by dp_rank without cloning.
    // Sort in-place by dp_rank, then pass contiguous slices to apply_events.
    batch.events.sort_by_key(|e| e.dp_rank);

    let mut total_applied = 0usize;
    let mut errors: Vec<String> = Vec::new();

    let mut i = 0;
    while i < batch.events.len() {
        let dp_rank = batch.events[i].dp_rank;
        let mut j = i + 1;
        while j < batch.events.len() && batch.events[j].dp_rank == dp_rank {
            j += 1;
        }

        match state
            .registry
            .apply_events(
                &batch.instance_id,
                dp_rank,
                &batch.events[i..j],
                batch.model_name.as_deref(),
                batch.tenant_id.as_deref(),
            )
            .await
        {
            Ok(n) => total_applied += n,
            Err(e) => {
                tracing::warn!(
                    instance_id = %batch.instance_id,
                    dp_rank,
                    error = %e,
                    "failed to apply events"
                );
                errors.push(format!("dp_rank={}: {}", dp_rank, e));
            }
        }
        i = j;
    }

    // Handle shutdown flag: the instance reports it is shutting down. Full
    // cleanup is done by an explicit /unregister call; here we just log.
    if batch.shutdown {
        tracing::info!(
            instance_id = %batch.instance_id,
            "shutdown flag set in events batch"
        );
        // The instance will be fully cleaned up by an explicit /unregister call.
        // Here we just log the intent.
    }

    if !errors.is_empty() {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({
                "status": "partial",
                "events_applied": total_applied,
                "errors": errors,
            })),
        );
    }

    (
        StatusCode::OK,
        Json(serde_json::json!({
            "status": "ok",
            "events_applied": total_applied,
        })),
    )
}

/// GET /health
async fn health_handler() -> &'static str {
    "OK"
}

/// GET /workers
async fn workers_handler(State(state): State<AppState>) -> Json<serde_json::Value> {
    let workers = state.registry.list_workers().await;
    let indexer = state.registry.indexer_summary();

    Json(serde_json::json!({
        "workers": workers,
        "indexer": indexer,
    }))
}
