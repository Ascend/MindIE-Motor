// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! ZMQ subscriber for KV event ingestion.
//!
//! Connects to engine-side ZMQ PUB sockets, receives multi-part msgpack
//! messages, and delegates event parsing and application to the [`events`]
//! module.  Supports Mooncake Master (legacy) and vLLM engine (native
//! msgspec) event formats.

use std::sync::Arc;
use std::time::Duration;

use tokio_util::sync::CancellationToken;

use crate::backend::MatchMode;
use crate::error::KvConductorError;
use crate::events::{self, ZmqBatchMap, ZmqEventMap};
use crate::indexer::Indexer;
use crate::protocols::StorageMedium;

/// Maximum backoff delay between reconnection attempts.
const MAX_RETRY_DELAY: Duration = Duration::from_secs(30);
/// Initial backoff delay.
const INITIAL_RETRY_DELAY: Duration = Duration::from_millis(100);

/// A ZMQ subscriber that connects to one KV event publisher endpoint.
///
/// Spawns a background task that reads multi-part ZMQ messages, parses them
/// as msgpack, normalizes events, and applies them to the indexer.
pub struct ZmqSubscriber {
    cancel: CancellationToken,
}

impl ZmqSubscriber {
    /// Create a new ZMQ subscriber and spawn a background task.
    #[allow(clippy::too_many_arguments)]
    pub fn connect(
        endpoint: String,
        model_name: String,
        tenant_id: String,
        indexer: Arc<Indexer>,
        block_size: u32,
        backend_id: String,
        dp_rank: u32,
        default_media: Vec<StorageMedium>,
        match_mode: MatchMode,
        hbm_ip_index: Option<crate::protocols::HbmIpIndex>,
    ) -> Result<Self, KvConductorError> {
        let cancel = CancellationToken::new();
        let cancel_clone = cancel.clone();

        // Validate up-front: try a one-shot connection to fail early on bad endpoints.
        let ctx = zmq::Context::new();
        Self::create_socket(&ctx, &endpoint)?;

        tracing::info!(
            %endpoint, %model_name, %tenant_id, %backend_id, dp_rank,
            block_size,
            "ZMQ subscriber starting with reconnect"
        );

        tokio::task::spawn_blocking(move || {
            subscriber_loop_with_reconnect(
                endpoint,
                model_name,
                tenant_id,
                indexer,
                block_size,
                backend_id,
                dp_rank,
                default_media,
                match_mode,
                hbm_ip_index,
                cancel_clone,
            );
        });

        Ok(Self { cancel })
    }

    /// Shut down the subscriber gracefully.
    pub fn shutdown(&self) {
        self.cancel.cancel();
    }

    /// Create and configure a ZMQ SUB socket connected to `endpoint`.
    pub(crate) fn create_socket(
        _ctx: &zmq::Context,
        endpoint: &str,
    ) -> Result<zmq::Socket, KvConductorError> {
        let socket = _ctx
            .socket(zmq::SUB)
            .map_err(|e| KvConductorError::Internal(format!("ZMQ SUB socket: {e}")))?;

        socket
            .connect(endpoint)
            .map_err(|e| KvConductorError::Internal(format!("ZMQ connect to {endpoint}: {e}")))?;

        socket
            .set_subscribe(b"")
            .map_err(|e| KvConductorError::Internal(format!("ZMQ subscribe: {e}")))?;

        socket
            .set_rcvtimeo(500)
            .map_err(|e| KvConductorError::Internal(format!("ZMQ rcvtimeo: {e}")))?;

        Ok(socket)
    }
}

impl Drop for ZmqSubscriber {
    fn drop(&mut self) {
        self.cancel.cancel();
    }
}

// ---------------------------------------------------------------------------
// Connection / reconnection loop
// ---------------------------------------------------------------------------

/// Outer loop: reconnect on fatal errors with exponential backoff.
#[allow(clippy::too_many_arguments)]
fn subscriber_loop_with_reconnect(
    endpoint: String,
    model_name: String,
    tenant_id: String,
    indexer: Arc<Indexer>,
    block_size: u32,
    backend_id: String,
    dp_rank: u32,
    default_media: Vec<StorageMedium>,
    match_mode: MatchMode,
    hbm_ip_index: Option<crate::protocols::HbmIpIndex>,
    cancel: CancellationToken,
) {
    let mut retry_delay = INITIAL_RETRY_DELAY;
    let ctx = zmq::Context::new();

    loop {
        if cancel.is_cancelled() {
            break;
        }

        match ZmqSubscriber::create_socket(&ctx, &endpoint) {
            Ok(socket) => {
                tracing::info!(%endpoint, %backend_id, dp_rank, "ZMQ subscriber connected");
                retry_delay = INITIAL_RETRY_DELAY;

                subscriber_loop(
                    socket,
                    model_name.clone(),
                    tenant_id.clone(),
                    indexer.clone(),
                    block_size,
                    backend_id.clone(),
                    dp_rank,
                    default_media.clone(),
                    match_mode,
                    hbm_ip_index.clone(),
                    cancel.clone(),
                );

                if cancel.is_cancelled() {
                    break;
                }
            }
            Err(e) => {
                tracing::warn!(%endpoint, %backend_id, dp_rank, error = %e, "ZMQ connect failed");
            }
        }

        if cancel.is_cancelled() {
            break;
        }

        tracing::warn!(
            %endpoint, %backend_id, dp_rank,
            delay_ms = retry_delay.as_millis(),
            "ZMQ reconnecting"
        );
        std::thread::sleep(retry_delay);
        retry_delay = (retry_delay * 2).min(MAX_RETRY_DELAY);
    }

    tracing::info!(%backend_id, dp_rank, "ZMQ subscriber reconnecting task shut down");
}

/// Background event loop: receive msgpack batches, normalize, apply to indexer.
#[allow(clippy::too_many_arguments)]
fn subscriber_loop(
    socket: zmq::Socket,
    model_name: String,
    tenant_id: String,
    indexer: Arc<Indexer>,
    _block_size: u32,
    backend_id: String,
    dp_rank: u32,
    default_media: Vec<StorageMedium>,
    match_mode: MatchMode,
    hbm_ip_index: Option<crate::protocols::HbmIpIndex>,
    cancel: CancellationToken,
) {
    let mut batch_count: u64 = 0;
    let mut event_count: u64 = 0;
    let mut parse_errors: u64 = 0;

    loop {
        if cancel.is_cancelled() {
            tracing::info!(
                %backend_id, dp_rank,
                batches = batch_count, events = event_count, parse_errors,
                "ZMQ subscriber shutting down"
            );
            break;
        }

        // Receive 3-part ZMQ message: [topic] [seq] [payload]
        let topic = match socket.recv_msg(zmq::DONTWAIT) {
            Ok(msg) => msg,
            Err(e) => {
                if zmq_errno_reasonable(&e) {
                    continue;
                }
                tracing::error!(%backend_id, dp_rank, "ZMQ recv topic error: {e}");
                break;
            }
        };

        let seq_msg = match socket.recv_msg(0) {
            Ok(msg) => msg,
            Err(e) => {
                tracing::error!(%backend_id, dp_rank, "ZMQ recv seq error: {e}");
                break;
            }
        };

        let payload_msg = match socket.recv_msg(0) {
            Ok(msg) => msg,
            Err(e) => {
                tracing::error!(%backend_id, dp_rank, "ZMQ recv payload error: {e}");
                break;
            }
        };

        drop(topic);
        drop(seq_msg);

        process_payload(
            &payload_msg,
            &indexer,
            &model_name,
            &tenant_id,
            &backend_id,
            dp_rank,
            &default_media,
            match_mode,
            &hbm_ip_index,
            &mut batch_count,
            &mut event_count,
            &mut parse_errors,
        );
    }
}

/// Dispatch a single msgpack payload to the correct format parser.
#[allow(clippy::too_many_arguments)]
fn process_payload(
    payload_msg: &[u8],
    indexer: &Indexer,
    model_name: &str,
    tenant_id: &str,
    backend_id: &str,
    dp_rank: u32,
    default_media: &[StorageMedium],
    match_mode: MatchMode,
    hbm_ip_index: &Option<crate::protocols::HbmIpIndex>,
    batch_count: &mut u64,
    event_count: &mut u64,
    parse_errors: &mut u64,
) {
    let payload_bytes: &[u8] = payload_msg;

    // Log the first byte so we can see which msgpack type is arriving.
    tracing::trace!(
        %backend_id, dp_rank,
        len = payload_bytes.len(),
        b0 = format!("0x{:02x}", payload_bytes.first().copied().unwrap_or(0)),
        b1 = format!("0x{:02x}", payload_bytes.get(1).copied().unwrap_or(0)),
        "ZMQ payload received"
    );

    // Format 1 — vLLM msgspec batch: [ts: f64, events: [...], dp_rank]
    if let Some((vllm_events, bdp)) = events::parse_vllm_batch(payload_bytes) {
        *batch_count += 1;
        for vllm_event in &vllm_events {
            *event_count += 1;
            if let Err(e) = events::apply_vllm_event(
                indexer,
                vllm_event,
                model_name,
                tenant_id,
                backend_id,
                bdp,
                dp_rank,
                default_media,
                match_mode,
                hbm_ip_index,
            ) {
                *parse_errors += 1;
                tracing::warn!(%backend_id, dp_rank, "vLLM event apply error: {e}");
            }
        }
    }
    // Format 2 — vLLM bare event
    else if let Some(vllm_event) = events::parse_vllm_bare(payload_bytes) {
        *event_count += 1;
        if let Err(e) = events::apply_vllm_event(
            indexer,
            &vllm_event,
            model_name,
            tenant_id,
            backend_id,
            dp_rank,
            dp_rank,
            default_media,
            match_mode,
            hbm_ip_index,
        ) {
            *parse_errors += 1;
            tracing::warn!(%backend_id, dp_rank, "vLLM event apply error: {e}");
        }
    }
    // Format 3 — Mooncake map batch
    else if let Ok(batch) = rmp_serde::from_slice::<ZmqBatchMap>(payload_bytes) {
        let bdp = batch.dp_rank.unwrap_or(0);
        *batch_count += 1;
        for zmq_event in &batch.events {
            *event_count += 1;
            if let Err(e) = events::apply_zmq_event(
                indexer,
                zmq_event,
                model_name,
                tenant_id,
                backend_id,
                bdp,
                dp_rank,
                default_media,
                match_mode,
                hbm_ip_index,
            ) {
                *parse_errors += 1;
                tracing::warn!(%backend_id, dp_rank, event_id = zmq_event.event_id, "{e}");
            }
        }
    }
    // Format 2 — Mooncake legacy array batch
    else if let Ok((_timestamp, events_vec, bdp)) =
        rmp_serde::from_slice::<(i64, Vec<ZmqEventMap>, u32)>(payload_bytes)
    {
        *batch_count += 1;
        for zmq_event in &events_vec {
            *event_count += 1;
            if let Err(e) = events::apply_zmq_event(
                indexer,
                zmq_event,
                model_name,
                tenant_id,
                backend_id,
                bdp,
                dp_rank,
                default_media,
                match_mode,
                hbm_ip_index,
            ) {
                *parse_errors += 1;
                tracing::warn!(%backend_id, dp_rank, event_id = zmq_event.event_id, "{e}");
            }
        }
    }
    // Format 3 — Bare Mooncake event
    else if let Ok(zmq_event) = rmp_serde::from_slice::<ZmqEventMap>(payload_bytes) {
        let bdp = zmq_event.dp_rank.unwrap_or(dp_rank);
        *event_count += 1;
        if let Err(e) = events::apply_zmq_event(
            indexer,
            &zmq_event,
            model_name,
            tenant_id,
            backend_id,
            bdp,
            dp_rank,
            default_media,
            match_mode,
            hbm_ip_index,
        ) {
            *parse_errors += 1;
            tracing::warn!(%backend_id, dp_rank, event_id = zmq_event.event_id, "{e}");
        }
    }
    // Parse failed
    else {
        *parse_errors += 1;
        let preview: String = payload_bytes
            .iter()
            .take(32)
            .map(|b| format!("{:02x}", b))
            .collect();
        tracing::warn!(
            %backend_id, dp_rank,
            payload_len = payload_bytes.len(),
            preview = %preview,
            "msgpack parse error: unknown format"
        );
    }
}

fn zmq_errno_reasonable(e: &zmq::Error) -> bool {
    matches!(e, zmq::Error::EAGAIN | zmq::Error::EINTR)
}

// ---------------------------------------------------------------------------
// Replay on registration
// ---------------------------------------------------------------------------

/// Connect to a vLLM engine's replay endpoint and request all buffered
/// KV events, applying them to the indexer.
///
/// vLLM's `ZmqEventPublisher` binds a ZMQ ROUTER on `replay_endpoint`.
/// The protocol:
///   1. DEALER sends  `[b"", start_seq: u64 BE]`
///   2. ROUTER replies `[b"", seq: u64 BE, msgpack_payload]` per buffered batch
///   3. End-of-stream: seq == 0xFFFFFFFFFFFFFFFF (-1 as signed i64)
///
/// Called during `/register` when the registration payload includes
/// a `replay_endpoint` field. Runs synchronously in the registration
/// handler (blocking).
pub fn replay_events(
    replay_endpoint: &str,
    model_name: &str,
    tenant_id: &str,
    _block_size: u32,
    indexer: &Indexer,
    backend_id: &str,
) {
    let ctx = zmq::Context::new();
    let socket = match ctx.socket(zmq::DEALER) {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!(%replay_endpoint, "replay: failed to create DEALER socket: {e}");
            return;
        }
    };
    if let Err(e) = socket.connect(replay_endpoint) {
        tracing::warn!(%replay_endpoint, "replay: connect failed: {e}");
        return;
    }
    let _ = socket.set_rcvtimeo(2000);
    let _ = socket.set_sndtimeo(2000);

    let start_seq: u64 = 0;
    let seq_be = start_seq.to_be_bytes();
    if socket.send(&b""[..], zmq::SNDMORE).is_err() || socket.send(&seq_be[..], 0).is_err() {
        tracing::warn!(%replay_endpoint, "replay: send request failed");
        return;
    }
    tracing::info!(%replay_endpoint, %backend_id, "replay: requested from seq=0");

    let mut batch_count: u64 = 0;
    let mut event_count: u64 = 0;
    loop {
        let delim = match socket.recv_msg(0) {
            Ok(m) => m,
            Err(_) => break,
        };
        let seq_msg = match socket.recv_msg(0) {
            Ok(m) => m,
            Err(_) => break,
        };
        let payload_msg = match socket.recv_msg(0) {
            Ok(m) => m,
            Err(_) => break,
        };
        drop(delim);

        let seq_bytes: &[u8] = &seq_msg;
        if seq_bytes.len() != 8 {
            tracing::warn!(%replay_endpoint, "replay: bad seq len {}", seq_bytes.len());
            break;
        }
        let seq = u64::from_be_bytes(seq_bytes.try_into().unwrap());
        if seq == u64::MAX {
            tracing::info!(%replay_endpoint, batches = batch_count, events = event_count,
                           "replay: complete");
            break;
        }

        let payload_bytes: &[u8] = &payload_msg;

        // Same order as subscriber_loop: vLLM first, then Mooncake fallbacks.
        if let Some((vllm_events, bdp)) = events::parse_vllm_batch(payload_bytes) {
            batch_count += 1;
            for vllm_event in &vllm_events {
                event_count += 1;
                if let Err(e) = events::apply_vllm_event(
                    indexer,
                    vllm_event,
                    model_name,
                    tenant_id,
                    backend_id,
                    bdp,
                    bdp,
                    &[],
                    MatchMode::None,
                    &None,
                ) {
                    tracing::warn!(%replay_endpoint, "replay vLLM event apply error: {e}");
                }
            }
        } else if let Some(vllm_event) = events::parse_vllm_bare(payload_bytes) {
            event_count += 1;
            if let Err(e) = events::apply_vllm_event(
                indexer,
                &vllm_event,
                model_name,
                tenant_id,
                backend_id,
                0,
                0,
                &[],
                MatchMode::None,
                &None,
            ) {
                tracing::warn!(%replay_endpoint, "replay vLLM event apply error: {e}");
            }
        } else if let Ok(batch) = rmp_serde::from_slice::<ZmqBatchMap>(payload_bytes) {
            let bdp = batch.dp_rank.unwrap_or(0);
            batch_count += 1;
            for zmq_event in &batch.events {
                event_count += 1;
                if let Err(e) = events::apply_zmq_event(
                    indexer,
                    zmq_event,
                    model_name,
                    tenant_id,
                    backend_id,
                    bdp,
                    bdp,
                    &[],
                    MatchMode::None,
                    &None,
                ) {
                    tracing::warn!(%replay_endpoint, event_id = zmq_event.event_id,
                                   "replay apply error: {e}");
                }
            }
        } else if let Ok((_ts, events_vec, bdp)) =
            rmp_serde::from_slice::<(i64, Vec<ZmqEventMap>, u32)>(payload_bytes)
        {
            batch_count += 1;
            for zmq_event in &events_vec {
                event_count += 1;
                if let Err(e) = events::apply_zmq_event(
                    indexer,
                    zmq_event,
                    model_name,
                    tenant_id,
                    backend_id,
                    bdp,
                    bdp,
                    &[],
                    MatchMode::None,
                    &None,
                ) {
                    tracing::warn!(%replay_endpoint, event_id = zmq_event.event_id,
                                   "replay apply error: {e}");
                }
            }
        } else if let Ok(zmq_event) = rmp_serde::from_slice::<ZmqEventMap>(payload_bytes) {
            let bdp = zmq_event.dp_rank.unwrap_or(0);
            event_count += 1;
            if let Err(e) = events::apply_zmq_event(
                indexer,
                &zmq_event,
                model_name,
                tenant_id,
                backend_id,
                bdp,
                bdp,
                &[],
                MatchMode::None,
                &None,
            ) {
                tracing::warn!(%replay_endpoint, event_id = zmq_event.event_id,
                               "replay apply error: {e}");
            }
        } else {
            tracing::warn!(%replay_endpoint, "replay msgpack parse error: unknown format");
        }
    }
}
