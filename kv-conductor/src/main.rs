// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! KV Conductor — Standalone KV cache indexer service for MindIE-PyMotor.
//!
//! Starts an HTTP server that maintains radix-tree-based KV cache indexes
//! per (model, tenant) pair, answering overlap queries to guide cache-aware
//! request routing decisions.

use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;

use clap::Parser;
use tracing_subscriber::EnvFilter;

use kv_conductor::registry::WorkerRegistry;
use kv_conductor::server::{create_router, AppState};

/// KV Conductor — Radix-tree-based KV cache indexer for MindIE-PyMotor.
#[derive(Parser, Debug)]
#[command(name = "kv-conductor")]
#[command(version = env!("CARGO_PKG_VERSION"))]
struct Cli {
    /// Host address to bind to
    #[arg(long, default_value = "0.0.0.0")]
    host: String,

    /// Port to listen on
    #[arg(long, short, default_value = "13333")]
    port: u16,
}

#[tokio::main]
async fn main() {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    let cli = Cli::parse();

    let host: IpAddr = cli.host.parse().expect("invalid host address");
    let addr = SocketAddr::new(host, cli.port);

    let registry = Arc::new(WorkerRegistry::new());
    let state = AppState { registry };
    let router = create_router(state);

    tracing::info!("KV conductor starting on {}", addr);

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind TCP listener");

    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("server error");

    tracing::info!("KV conductor shut down");
}

async fn shutdown_signal() {
    tokio::signal::ctrl_c()
        .await
        .expect("failed to install Ctrl+C handler");
    tracing::info!("received shutdown signal");
}
