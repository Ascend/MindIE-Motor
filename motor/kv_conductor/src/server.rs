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
//! - `POST /query`          — Query KV cache overlap scores by token IDs
//! - `POST /query_by_hash`  — Query KV cache overlap scores by pre-computed hashes
//! - `POST /events`         — Ingest KV cache events from workers
//! - `GET /health`          — Health check
//! - `GET /workers`         — List registered workers (debug)

use std::sync::Arc;

use axum::{
    extract::State,
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use tower_http::cors::{Any, CorsLayer};
use tower_http::trace::TraceLayer;

/// Maximum request body size (16 MB). Large queries (402400+ token IDs)
/// exceed axum's default 2 MB limit.
const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

use crate::error::KvConductorError;
use crate::protocols::*;
use crate::registry::WorkerRegistry;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub registry: Arc<WorkerRegistry>,
    /// Per-medium block scoring weights.
    pub scoring: ScoringConfig,
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

    // Raise body limit for query/events endpoints — DeepSeek V4 queries
    // carry 400K+ token IDs (~2.4 MB JSON body).
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
        Err(KvConductorError::DuplicateRegistration {
            instance_id,
            dp_rank,
        }) => (
            StatusCode::CONFLICT,
            Json(serde_json::json!({
                "error": format!("instance {} dp_rank {} already registered", instance_id, dp_rank)
            })),
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

/// POST /query
///
/// Request body: `{ "model": "...", "block_size": 128, "token_ids": [...], "tenant_id": "default" }`
///
/// Response: `{ "<tenant_id>": { "<instance_id>": { "longest_matched": N, "DP": { "<rank>": N } } } }`
async fn query_handler(
    State(state): State<AppState>,
    Json(req): Json<QueryRequest>,
) -> (StatusCode, Json<serde_json::Value>) {
    tracing::debug!(
        model = %req.model,
        tenant = %req.tenant_id,
        num_tokens = req.token_ids.len(),
        "query request"
    );

    match state.registry.query(&req).await {
        Ok(response) => (
            StatusCode::OK,
            Json(serde_json::to_value(response).unwrap_or_default()),
        ),
        Err(KvConductorError::NoIndexer {
            model_name,
            tenant_id,
        }) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({
                "error": format!("no indexer for model={} tenant={}", model_name, tenant_id)
            })),
        ),
        Err(KvConductorError::NoWorkers {
            model_name: _,
            tenant_id,
        }) => (
            StatusCode::OK,
            // Return empty response structure matching expected format
            Json(serde_json::json!({
                tenant_id: {}
            })),
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": e.to_string()})),
        ),
    }
}

/// POST /query_by_hash
///
/// Same semantics as `/query` but accepts pre-computed block hashes instead
/// of raw token IDs, avoiding redundant XXH3 computation.
async fn query_by_hash_handler(
    State(state): State<AppState>,
    Json(req): Json<QueryByHashRequest>,
) -> (StatusCode, Json<serde_json::Value>) {
    tracing::debug!(
        model = %req.model,
        tenant = %req.tenant_id,
        num_hashes = req.block_hashes.len(),
        "query_by_hash request"
    );

    match state.registry.query_by_hash(&req).await {
        Ok(response) => (
            StatusCode::OK,
            Json(serde_json::to_value(response).unwrap_or_default()),
        ),
        Err(KvConductorError::NoIndexer {
            model_name,
            tenant_id,
        }) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({
                "error": format!("no indexer for model={} tenant={}", model_name, tenant_id)
            })),
        ),
        Err(KvConductorError::NoWorkers {
            model_name: _,
            tenant_id,
        }) => (
            StatusCode::OK,
            Json(serde_json::json!({
                tenant_id: {}
            })),
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": e.to_string()})),
        ),
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

    // Handle shutdown flag: unregister the instance if it's shutting down
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
