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
use tracing_subscriber::fmt::time::OffsetTime;
use tracing_subscriber::EnvFilter;

use kv_conductor::protocols::ScoringConfig;
use kv_conductor::registry::WorkerRegistry;
use kv_conductor::server::{create_router, AppState};

/// KV Conductor — Radix-tree-based KV cache indexer for MindIE-PyMotor.
#[derive(Parser, Debug)]
#[command(name = "kv-conductor")]
#[command(version = env!("CARGO_PKG_VERSION"))]
struct Cli {
    /// Host address to bind to (IPv4/IPv6, dual-stack by default)
    #[arg(long, default_value = "::")]
    host: String,

    /// Port to listen on
    #[arg(long, short, default_value = "13333")]
    port: u16,

    /// Score per matched HBM/XPU block
    #[arg(long, default_value = "3")]
    hbm_weight: u32,

    /// Score per matched CPU block
    #[arg(long, default_value = "2")]
    cpu_weight: u32,

    /// Score per matched disk block
    #[arg(long, default_value = "1")]
    disk_weight: u32,
}

#[tokio::main]
async fn main() {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .with_timer(OffsetTime::new(
            time::UtcOffset::from_hms(8, 0, 0).expect("invalid UTC+8 offset"),
            time::format_description::well_known::Rfc3339,
        ))
        .init();

    let cli = Cli::parse();

    let host: IpAddr = cli.host.parse().expect("invalid host address");
    let addr = SocketAddr::new(host, cli.port);

    let scoring = ScoringConfig {
        hbm_weight: cli.hbm_weight,
        cpu_weight: cli.cpu_weight,
        disk_weight: cli.disk_weight,
    };
    tracing::info!(
        hbm_weight = scoring.hbm_weight,
        cpu_weight = scoring.cpu_weight,
        disk_weight = scoring.disk_weight,
        "scoring config"
    );

    let registry = Arc::new(WorkerRegistry::new(scoring.clone()));
    let state = AppState { registry, scoring };
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
