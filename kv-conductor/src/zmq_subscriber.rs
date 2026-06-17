// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! ZMQ SUB subscriber for RFC #1527 KV cache events.
//!
//! Connects to Mooncake master (or vLLM/SGLang engine) ZMQ PUB endpoints and
//! ingests KV cache events into the local radix tree indexer.

use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use tokio_util::sync::CancellationToken;

use crate::backend::MatchMode;
use crate::error::KvConductorError;
use crate::indexer::Indexer;
use crate::protocols::*;

/// Maximum backoff delay between reconnection attempts.
const MAX_RETRY_DELAY: Duration = Duration::from_secs(30);
/// Initial backoff delay.
const INITIAL_RETRY_DELAY: Duration = Duration::from_millis(100);

/// A u64 that can be deserialized from multiple msgpack representations:
///   - integer (u64, i64, u32, …)
///   - decimal string   "12345678901234567890"
///   - hex string       "0xABCD1234…" or "ABCD1234…"
///   - binary bytes     up to 8 bytes, big-endian (vLLM BlockHash compat)
#[derive(Debug, Clone, Copy)]
struct FlexHash(u64);

impl<'de> Deserialize<'de> for FlexHash {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct FlexHashVisitor;
        impl<'de> serde::de::Visitor<'de> for FlexHashVisitor {
            type Value = FlexHash;

            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a u64, decimal/hex string, or up to 8 bytes")
            }

            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<FlexHash, E> {
                Ok(FlexHash(v))
            }

            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<FlexHash, E> {
                if v < 0 {
                    return Err(E::custom(format!("negative hash: {v}")));
                }
                Ok(FlexHash(v as u64))
            }

            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<FlexHash, E> {
                let s = v.trim();
                // 1. "0x" / "0X" prefix → always hex
                if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
                    return u64::from_str_radix(hex, 16)
                        .map(FlexHash)
                        .map_err(|e| E::custom(format!("invalid hex hash '{v}': {e}")));
                }
                // 2. Try decimal (handles pure-digit hashes)
                if let Ok(n) = s.parse::<u64>() {
                    return Ok(FlexHash(n));
                }
                // 3. Fallback: hex without prefix (e.g. "FF", "ABCD")
                u64::from_str_radix(s, 16).map(FlexHash).map_err(|_| {
                    E::custom(format!(
                        "invalid hash '{v}': expected u64, hex, or 0x-prefixed"
                    ))
                })
            }

            fn visit_bytes<E: serde::de::Error>(self, v: &[u8]) -> Result<FlexHash, E> {
                bytes_to_flex(v)
            }

            fn visit_byte_buf<E: serde::de::Error>(self, v: Vec<u8>) -> Result<FlexHash, E> {
                bytes_to_flex(&v)
            }
        }

        fn bytes_to_flex<E: serde::de::Error>(v: &[u8]) -> Result<FlexHash, E> {
            if v.len() > 8 {
                return Err(E::custom(format!("hash bytes too long: {} > 8", v.len())));
            }
            let mut buf = [0u8; 8];
            buf[8 - v.len()..].copy_from_slice(v); // pad left for big-endian
            Ok(FlexHash(u64::from_be_bytes(buf)))
        }

        deserializer.deserialize_any(FlexHashVisitor)
    }
}

/// Deserialized msgpack event map from a ZMQ PUB frame.
#[derive(Debug, Deserialize)]
struct ZmqEventMap {
    #[serde(default)]
    event_id: u64,
    #[serde(default)]
    #[allow(dead_code)]
    timestamp: Option<i64>,
    #[serde(default, alias = "event_type")]
    event_type: Option<String>,
    /// Legacy compat alias for event_type.
    #[serde(default)]
    #[serde(rename = "type")]
    legacy_type: Option<String>,
    #[serde(default)]
    model_name: Option<String>,
    #[serde(default)]
    block_size: Option<u32>,
    #[serde(default)]
    tenant_id: Option<String>,
    #[serde(default)]
    backend_id: Option<String>,
    #[serde(default)]
    medium: Option<String>,
    #[serde(default)]
    dp_rank: Option<u32>,
    #[serde(default)]
    seq_hashes: Option<Vec<FlexHash>>,
    #[serde(default)]
    block_hashes: Option<Vec<FlexHash>>,
}

/// A ZMQ subscriber that connects to one KV event publisher endpoint.
///
/// Spawns a background task that reads multi-part ZMQ messages, parses them
/// as msgpack, normalizes events, and applies them to the indexer.
pub struct ZmqSubscriber {
    cancel: CancellationToken,
}

impl ZmqSubscriber {
    /// Create a new ZMQ subscriber and spawn a background task.
    ///
    /// - `endpoint`: ZMQ PUB connect address, e.g. `tcp://<host>:5557`
    /// - `indexer`: shared indexer to apply events into
    /// - `block_size`: KV block size for this model
    /// - `backend_id`: RFC #1527 backend_id for this subscriber
    /// - `dp_rank`: data-parallel rank from registration
    /// - `default_media`: storage media this endpoint serves; events without
    ///   an explicit `medium` field are applied to all of them.
    /// - `match_mode`: how pool events are matched to HBM DPs
    ///   (None = per-DP subscriber; IpOnly = IP → all DPs; IpAndDpRank = exact match).
    /// - `hbm_ip_index`: IP → (instance_id, dp_rank) lookup for auto-attach.
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
        hbm_ip_index: Option<HbmIpIndex>,
    ) -> Result<Self, KvConductorError> {
        let cancel = CancellationToken::new();
        let cancel_clone = cancel.clone();

        // Validate up-front: try a one-shot connection to fail early on bad
        // endpoints. The context lifetime is fine because the socket is
        // dropped before this function returns.
        let ctx = zmq::Context::new();
        Self::create_socket(&ctx, &endpoint)?;

        tracing::info!(
            %endpoint,
            %model_name,
            %tenant_id,
            %backend_id,
            dp_rank,
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
    ///
    /// The `_ctx` parameter keeps the ZMQ context alive — the returned socket
    /// borrows from it internally, so the context must not be dropped before
    /// the socket.
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

        // Subscribe to all topics (Mooncake uses empty topic)
        socket
            .set_subscribe(b"")
            .map_err(|e| KvConductorError::Internal(format!("ZMQ subscribe: {e}")))?;

        // Set receive timeout for interruptible recv
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
    hbm_ip_index: Option<HbmIpIndex>,
    cancel: CancellationToken,
) {
    let mut retry_delay = INITIAL_RETRY_DELAY;
    // Keep the ZMQ context alive across reconnection attempts so that the
    // underlying C context isn't created/destroyed on every cycle.
    let ctx = zmq::Context::new();

    loop {
        if cancel.is_cancelled() {
            break;
        }

        match ZmqSubscriber::create_socket(&ctx, &endpoint) {
            Ok(socket) => {
                tracing::info!(%endpoint, %backend_id, dp_rank, "ZMQ subscriber connected");
                // Reset backoff on successful connection
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

                // subscriber_loop returned — either cancelled or connection lost
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
            %endpoint,
            %backend_id,
            dp_rank,
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
    block_size: u32,
    backend_id: String,
    dp_rank: u32,
    default_media: Vec<StorageMedium>,
    match_mode: MatchMode,
    hbm_ip_index: Option<HbmIpIndex>,
    cancel: CancellationToken,
) {
    let mut batch_count: u64 = 0;
    let mut event_count: u64 = 0;
    let mut parse_errors: u64 = 0;

    loop {
        if cancel.is_cancelled() {
            tracing::info!(
                %backend_id,
                dp_rank,
                batches = batch_count,
                events = event_count,
                parse_errors,
                "ZMQ subscriber shutting down"
            );
            break;
        }

        // Receive 3-part ZMQ message: [topic] [seq] [payload]
        let topic = match socket.recv_msg(zmq::DONTWAIT) {
            Ok(msg) => msg,
            Err(e) => {
                // Timeout or interrupt — just retry
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

        // Parse msgpack payload: [timestamp_ms, [events], dp_rank]
        let payload_bytes: &[u8] = &payload_msg;
        let parsed: Result<(i64, Vec<ZmqEventMap>, u32), _> = rmp_serde::from_slice(payload_bytes);

        match parsed {
            Ok((_timestamp, events, batch_dp_rank)) => {
                batch_count += 1;
                for zmq_event in &events {
                    event_count += 1;
                    if let Err(e) = apply_zmq_event(
                        &indexer,
                        zmq_event,
                        &model_name,
                        &tenant_id,
                        block_size,
                        &backend_id,
                        batch_dp_rank,
                        dp_rank,
                        &default_media,
                        match_mode,
                        &hbm_ip_index,
                    ) {
                        parse_errors += 1;
                        tracing::warn!(
                            %backend_id,
                            dp_rank,
                            event_id = zmq_event.event_id,
                            "{e}"
                        );
                    }
                }
            }
            Err(e) => {
                parse_errors += 1;
                tracing::warn!(
                    %backend_id,
                    dp_rank,
                    "msgpack parse error: {e}"
                );
            }
        }
    }
}

/// Check if a ZMQ error is a non-fatal timeout or interrupt.
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
/// handler (blocking); the ZMQ DEALER has a short timeout so a
/// non-responsive engine won't stall registration.
pub fn replay_events(
    replay_endpoint: &str,
    model_name: &str,
    tenant_id: &str,
    block_size: u32,
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
    // Short timeout — replay is best-effort; don't block registration.
    let _ = socket.set_rcvtimeo(2000);
    let _ = socket.set_sndtimeo(2000);

    // Request replay from sequence 0.
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
        // Receive: [b"", seq: u64 BE, msgpack_payload]
        let delim = match socket.recv_msg(0) {
            Ok(m) => m,
            Err(_) => break, // timeout or error — done
        };
        // delimiter should be empty
        let seq_msg = match socket.recv_msg(0) {
            Ok(m) => m,
            Err(_) => break,
        };
        let payload_msg = match socket.recv_msg(0) {
            Ok(m) => m,
            Err(_) => break,
        };

        drop(delim);

        // Parse sequence number
        let seq_bytes: &[u8] = &seq_msg;
        if seq_bytes.len() != 8 {
            tracing::warn!(%replay_endpoint, "replay: bad seq len {}", seq_bytes.len());
            break;
        }
        let seq = u64::from_be_bytes(seq_bytes.try_into().unwrap());
        // End-of-stream marker: -1 as signed i64 = 0xFFFFFFFFFFFFFFFF
        if seq == u64::MAX {
            tracing::info!(%replay_endpoint, batches = batch_count, events = event_count,
                           "replay: complete");
            break;
        }

        // Parse msgpack payload: [timestamp_ms, [events], dp_rank]
        let payload_bytes: &[u8] = &payload_msg;
        let parsed: Result<(i64, Vec<ZmqEventMap>, u32), _> = rmp_serde::from_slice(payload_bytes);
        match parsed {
            Ok((_timestamp, events, batch_dp_rank)) => {
                batch_count += 1;
                for zmq_event in &events {
                    event_count += 1;
                    if let Err(e) = apply_zmq_event(
                        indexer,
                        zmq_event,
                        model_name,
                        tenant_id,
                        block_size,
                        backend_id,
                        batch_dp_rank,
                        batch_dp_rank, // subscriber_dp_rank = batch_dp_rank for replay
                        &[],           // default_media — events carry their own medium
                        MatchMode::None,
                        &None, // no IP index for replay
                    ) {
                        tracing::warn!(%replay_endpoint, event_id = zmq_event.event_id, "replay apply error: {e}");
                    }
                }
            }
            Err(e) => {
                tracing::warn!(%replay_endpoint, "replay msgpack parse error: {e}");
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use rmp_serde::from_slice;

    /// Manually construct a msgpack array containing one binary blob.
    /// msgpack format: 0x91 = array(1), 0xC4 = bin8, then len byte + data.
    fn msgpack_bin(data: &[u8]) -> Vec<u8> {
        let mut buf = vec![0x91, 0xC4, data.len() as u8];
        buf.extend_from_slice(data);
        buf
    }

    #[test]
    fn test_flex_hash_u64() {
        let data = rmp_serde::to_vec(&vec![42u64, 18446744073709551615u64]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 42);
        assert_eq!(hashes[1].0, u64::MAX);
    }

    #[test]
    fn test_flex_hash_decimal_string() {
        let data = rmp_serde::to_vec(&vec!["42"]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 42);
    }

    #[test]
    fn test_flex_hash_hex_string() {
        let data = rmp_serde::to_vec(&vec!["0x2A"]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 0x2A);
    }

    #[test]
    fn test_flex_hash_hex_string_no_prefix() {
        let data = rmp_serde::to_vec(&vec!["FF"]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 0xFF);
    }

    #[test]
    fn test_flex_hash_bytes() {
        let data = msgpack_bin(&[0x00, 0x00, 0x00, 0x2A]); // 4 bytes BE = 42
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 42);
    }

    #[test]
    fn test_flex_hash_bytes_max() {
        let data = msgpack_bin(&[0xFFu8; 8]);
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, u64::MAX);
    }

    #[test]
    fn test_flex_hash_i64_negative_rejected() {
        let data = rmp_serde::to_vec(&vec![-1i64]).unwrap();
        let result: Result<Vec<FlexHash>, _> = from_slice(&data);
        assert!(result.is_err());
    }

    #[test]
    fn test_flex_hash_bytes_too_long_rejected() {
        let packed = rmp_serde::to_vec(&vec![vec![0u8; 9]]).unwrap();
        let result: Result<Vec<FlexHash>, _> = from_slice(&packed);
        assert!(result.is_err());
    }

    #[test]
    fn test_flex_hash_integrated_in_zmq_event_map() {
        // Simulates a Mooncake Master event with hex string hashes
        let event = serde_json::json!({
            "event_id": 1,
            "event_type": "stored",
            "medium": "cpu",
            "seq_hashes": ["0xABCD", "12345"],
            "block_hashes": [100, 200]
        });
        let packed = rmp_serde::to_vec(&event).unwrap();
        let map: ZmqEventMap = from_slice(&packed).unwrap();
        let seq: Vec<u64> = map.seq_hashes.unwrap().iter().map(|h| h.0).collect();
        assert_eq!(seq, vec![0xABCD, 12345]);
        let blk: Vec<u64> = map.block_hashes.unwrap().iter().map(|h| h.0).collect();
        assert_eq!(blk, vec![100, 200]);
    }
}

/// Apply a single parsed ZMQ event to the indexer.
#[allow(clippy::too_many_arguments)]
fn apply_zmq_event(
    indexer: &Indexer,
    zmq_event: &ZmqEventMap,
    model_name: &str,
    tenant_id: &str,
    block_size: u32,
    backend_id: &str,
    batch_dp_rank: u32,
    _subscriber_dp_rank: u32,
    default_media: &[StorageMedium],
    match_mode: MatchMode,
    hbm_ip_index: &Option<HbmIpIndex>,
) -> Result<(), KvConductorError> {
    // Determine event type
    let event_type = zmq_event
        .event_type
        .as_deref()
        .or(zmq_event.legacy_type.as_deref())
        .unwrap_or("unknown");

    let is_stored = event_type.contains("stored") || event_type.contains("Stored");
    let is_removed = event_type.contains("removed") || event_type.contains("Removed");
    let is_cleared = event_type.contains("cleared")
        || event_type.contains("Cleared")
        || event_type.contains("AllBlocksCleared");

    // Collect seq_hashes from both RFC and legacy fields
    let mut seq_hashes: Vec<SequenceBlockHash> = Vec::new();
    if let Some(ref hashes) = zmq_event.seq_hashes {
        seq_hashes.extend(hashes.iter().map(|h| SequenceBlockHash(h.0)));
    }
    if let Some(ref hashes) = zmq_event.block_hashes {
        seq_hashes.extend(hashes.iter().map(|h| SequenceBlockHash(h.0)));
    }

    if seq_hashes.is_empty() {
        return Ok(()); // Nothing to do
    }

    // Determine target storage media.
    // If the event specifies a medium explicitly, use only that one.
    // Otherwise broadcast to all default_media configured for this endpoint
    // (e.g. a DDR/SSD port may serve both CPU and DISK).
    let target_media: Vec<StorageMedium> = if let Some(ref m) = zmq_event.medium {
        vec![StorageMedium::parse(m)]
    } else {
        default_media.to_vec()
    };

    // Use backend_id from event or fall back to the subscriber's configured backend_id
    let be_id = zmq_event.backend_id.as_deref().unwrap_or(backend_id);

    // Three-tier dp_rank fallback:
    //   1. Per-event dp_rank (most specific — e.g. aggregated endpoints)
    //   2. Batch-level dp_rank from the ZMQ protocol envelope
    //      (always present in the [timestamp, events, dp_rank] wire tuple)
    //   3. Subscriber-level dp_rank from registration (ultimate default;
    //      consumed by higher layers for diagnostics when it differs from
    //      the resolved value)
    let dp_rank = zmq_event.dp_rank.unwrap_or(batch_dp_rank);

    // Use model_name from event if present; otherwise subscriber's default
    let mn = zmq_event.model_name.as_deref().unwrap_or(model_name);

    let tid = zmq_event.tenant_id.as_deref().unwrap_or(tenant_id);

    // Get or create indexer entry (may differ from subscriber's default model/tenant)
    let entry = indexer.get_or_create(mn, tid, zmq_event.block_size.unwrap_or(block_size));

    // Resolve target workers.
    //   None (YuanRong):   one WorkerKey per medium, fixed dp_rank.
    //   IpOnly (Mooncake): match backend_id=IP → all DPs on that node.
    //   IpAndDpRank (Memcache): match backend_id=IP + dp_rank → exact DP.
    let target_workers: Vec<WorkerKey> = if match_mode == MatchMode::None {
        // Per-DP subscriber: one worker per medium.
        target_media
            .iter()
            .map(|&medium| WorkerKey {
                instance_id: be_id.to_string(),
                backend_id: be_id.to_string(),
                dp_rank,
                medium,
            })
            .collect()
    } else {
        // Pool subscriber: delegate to MatchMode for IP look-up.
        match_mode.resolve_workers(hbm_ip_index.as_ref(), be_id, dp_rank, &target_media)
    };

    // Apply the event to each target worker
    for worker in &target_workers {
        // Build a normalized event and apply to the tree
        if is_stored {
            // Mooncake events are flat (no parent_hash) — insert seq_hashes as root children.
            // Since there's no tokens_hash from Mooncake, we use seq_hash as the tree key.
            let store_data = KvCacheStoreData {
                parent_hash: None,
                start_position: None,
                blocks: seq_hashes
                    .iter()
                    .map(|h| KvCacheStoredBlockData {
                        block_hash: h.0,
                        tokens_hash: h.0,
                    })
                    .collect(),
            };

            entry.apply_event(worker, &KvCacheEventData::Stored(store_data))?;
        } else if is_removed {
            let block_hashes: Vec<u64> = seq_hashes.iter().map(|h| h.0).collect();
            entry.apply_event(worker, &KvCacheEventData::Removed { block_hashes })?;
        } else if is_cleared {
            entry.apply_event(worker, &KvCacheEventData::Cleared)?;
        }
    }

    Ok(())
}
