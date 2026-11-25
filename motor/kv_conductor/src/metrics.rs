// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Lock-free Prometheus metrics for the KV Conductor service.
//!
//! Exposed on `GET /metrics` in Prometheus text exposition format 0.0.4.
//! Uses only standard-library atomics so no additional crate dependencies are needed.

use std::fmt::{Display, Write as _};
use std::sync::atomic::{AtomicU64, Ordering};

/// Latency histogram buckets in seconds (100 us .. 1 s).
const LATENCY_BUCKETS: [f64; 13] = [
    0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
];

/// Coverage ratio histogram buckets in [0.0, 1.0].
const RATIO_BUCKETS: [f64; 7] = [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0];

/// Event sources that carry sequence numbers.
const SEQ_SOURCES: [MetricEventSource; 3] = [
    MetricEventSource::ZmqEngine,
    MetricEventSource::ZmqPool,
    MetricEventSource::Replay,
];

/// Lock-free fixed-bucket histogram storing counts as `AtomicU64` and sum as
/// micro-scaled integer (`f64 * 1_000_000`) to avoid CAS loops on f64 bits.
pub struct AtomicHistogram<const N: usize> {
    bounds: [f64; N],
    buckets: [AtomicU64; N],
    inf: AtomicU64,
    count: AtomicU64,
    sum_micros: AtomicU64,
}

impl<const N: usize> AtomicHistogram<N> {
    pub fn new(bounds: [f64; N]) -> Self {
        Self {
            bounds,
            buckets: zeros(),
            inf: AtomicU64::new(0),
            count: AtomicU64::new(0),
            sum_micros: AtomicU64::new(0),
        }
    }

    pub fn observe(&self, value: f64) {
        let clamped = if value.is_finite() && value >= 0.0 {
            value
        } else {
            0.0
        };
        for (idx, bound) in self.bounds.iter().enumerate() {
            if clamped <= *bound {
                self.buckets[idx].fetch_add(1, Ordering::Relaxed);
            }
        }
        self.inf.fetch_add(1, Ordering::Relaxed);
        self.count.fetch_add(1, Ordering::Relaxed);
        let scaled = (clamped * 1_000_000.0).round() as u64;
        self.sum_micros.fetch_add(scaled, Ordering::Relaxed);
    }

    fn render_samples(&self, out: &mut String, name: &str, labels: &str) {
        let prefix = if labels.is_empty() {
            String::new()
        } else {
            format!("{labels},")
        };
        for (idx, bound) in self.bounds.iter().enumerate() {
            let v = load(&self.buckets[idx]);
            let _ = writeln!(out, "{name}_bucket{{{prefix}le=\"{bound}\"}} {v}");
        }
        let inf = load(&self.inf);
        let _ = writeln!(out, "{name}_bucket{{{prefix}le=\"+Inf\"}} {inf}");
        let sum = format!("{:.6}", load(&self.sum_micros) as f64 / 1_000_000.0);
        write_sample(out, &format!("{name}_sum"), labels, sum);
        write_sample(out, &format!("{name}_count"), labels, load(&self.count));
    }
}

fn zeros<const N: usize>() -> [AtomicU64; N] {
    std::array::from_fn(|_| AtomicU64::new(0))
}

fn load(v: &AtomicU64) -> u64 {
    v.load(Ordering::Relaxed)
}

/// Write the `# HELP` and `# TYPE` lines of one metric family.
fn write_header(out: &mut String, name: &str, kind: &str, help: &str) {
    let _ = writeln!(out, "# HELP {name} {help}");
    let _ = writeln!(out, "# TYPE {name} {kind}");
}

/// Write one sample line. `labels` is the text inside `{...}`, or empty for none.
fn write_sample(out: &mut String, name: &str, labels: &str, value: impl Display) {
    if labels.is_empty() {
        let _ = writeln!(out, "{name} {value}");
    } else {
        let _ = writeln!(out, "{name}{{{labels}}} {value}");
    }
}

/// Query endpoint kind for labels.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueryEndpoint {
    Query,
    QueryByHash,
}

impl QueryEndpoint {
    const ALL: [Self; 2] = [Self::Query, Self::QueryByHash];

    fn as_str(self) -> &'static str {
        match self {
            Self::Query => "query",
            Self::QueryByHash => "query_by_hash",
        }
    }
}

/// Query stage kind for latency histograms.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueryStage {
    Hash,
    Match,
    Build,
}

impl QueryStage {
    const ALL: [Self; 3] = [Self::Hash, Self::Match, Self::Build];

    fn as_str(self) -> &'static str {
        match self {
            Self::Hash => "hash",
            Self::Match => "match",
            Self::Build => "build",
        }
    }
}

/// Query result classification.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueryResultKind {
    Hit,
    Miss,
    Empty,
    BadRequest,
    NoIndexer,
    Error,
}

impl QueryResultKind {
    const ALL: [Self; 6] = [
        Self::Hit,
        Self::Miss,
        Self::Empty,
        Self::BadRequest,
        Self::NoIndexer,
        Self::Error,
    ];

    fn as_str(self) -> &'static str {
        match self {
            Self::Hit => "hit",
            Self::Miss => "miss",
            Self::Empty => "empty",
            Self::BadRequest => "bad_request",
            Self::NoIndexer => "no_indexer",
            Self::Error => "error",
        }
    }
}

/// Query error reason.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueryErrorKind {
    BadRequest,
    NoIndexer,
    Internal,
}

impl QueryErrorKind {
    const ALL: [Self; 3] = [Self::BadRequest, Self::NoIndexer, Self::Internal];

    fn as_str(self) -> &'static str {
        match self {
            Self::BadRequest => "bad_request",
            Self::NoIndexer => "no_indexer",
            Self::Internal => "internal",
        }
    }
}

/// Event ingestion source.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MetricEventSource {
    Http,
    ZmqEngine,
    ZmqPool,
    Replay,
}

impl MetricEventSource {
    const ALL: [Self; 4] = [Self::Http, Self::ZmqEngine, Self::ZmqPool, Self::Replay];

    fn as_str(self) -> &'static str {
        match self {
            Self::Http => "http",
            Self::ZmqEngine => "zmq_engine",
            Self::ZmqPool => "zmq_pool",
            Self::Replay => "replay",
        }
    }
}

/// Event processing status.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MetricEventStatus {
    Received,
    Applied,
    ParseError,
    ApplyError,
    Dropped,
}

impl MetricEventStatus {
    const ALL: [Self; 5] = [
        Self::Received,
        Self::Applied,
        Self::ParseError,
        Self::ApplyError,
        Self::Dropped,
    ];

    fn as_str(self) -> &'static str {
        match self {
            Self::Received => "received",
            Self::Applied => "applied",
            Self::ParseError => "parse_error",
            Self::ApplyError => "apply_error",
            Self::Dropped => "dropped",
        }
    }
}

/// Sequence anomaly kind for sequence-numbered event streams.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SeqAnomalyKind {
    Gap,
    Duplicate,
    OutOfOrder,
}

impl SeqAnomalyKind {
    const ALL: [Self; 3] = [Self::Gap, Self::Duplicate, Self::OutOfOrder];

    fn as_str(self) -> &'static str {
        match self {
            Self::Gap => "gap",
            Self::Duplicate => "duplicate",
            Self::OutOfOrder => "out_of_order",
        }
    }
}

/// Storage tier for hit / predicted coverage metrics.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MetricTier {
    Hbm,
    Cpu,
    Disk,
    Any,
}

impl MetricTier {
    const ALL: [Self; 4] = [Self::Hbm, Self::Cpu, Self::Disk, Self::Any];

    fn as_str(self) -> &'static str {
        match self {
            Self::Hbm => "hbm",
            Self::Cpu => "cpu",
            Self::Disk => "disk",
            Self::Any => "any",
        }
    }
}

/// Per-query profiling and hit observation payload produced by the indexer.
#[derive(Debug, Clone, Copy, Default)]
pub struct QueryObservation {
    pub hash_secs: f64,
    pub match_secs: f64,
    pub build_secs: f64,
    pub query_tokens: u64,
    pub best_hbm_blocks: u64,
    pub best_cpu_blocks: u64,
    pub best_disk_blocks: u64,
    pub best_any_blocks: u64,
    pub best_hbm_tokens: u64,
    pub best_cpu_tokens: u64,
    pub best_disk_tokens: u64,
    pub best_any_tokens: u64,
}

/// Point-in-time snapshot of live registry & indexer gauges rendered on scrape.
#[derive(Debug, Clone, Copy, Default)]
pub struct ConductorGaugeSnapshot {
    pub registered_workers: usize,
    pub effective_workers: usize,
    pub index_entries: usize,
    pub hbm_blocks: usize,
    pub cpu_blocks: usize,
    pub disk_blocks: usize,
    pub offload_entries: usize,
    pub content_entries: usize,
    pub pending_pool_entries: usize,
    pub index_valid: bool,
}

/// Central metrics container owned by [`crate::registry::WorkerRegistry`].
pub struct ConductorMetrics {
    // [endpoint: 2][result: 6]
    queries: [[AtomicU64; 6]; 2],
    // [endpoint: 2][error_reason: 3]
    query_errors: [[AtomicU64; 3]; 2],
    // [endpoint: 2]
    query_duration: [AtomicHistogram<13>; 2],
    // [endpoint: 2][stage: 3]
    query_stage_duration: [[AtomicHistogram<13>; 3]; 2],

    // [source: 4][status: 5]
    events: [[AtomicU64; 5]; 4],
    // [source: 4][0=applied, 1=error]
    event_batches: [[AtomicU64; 2]; 4],
    // [source: 4]
    zmq_reconnects: [AtomicU64; 4],
    // [source: 4][kind: 3]
    seq_anomalies: [[AtomicU64; 3]; 4],
    // [source: 4]
    seq_gap_messages: [AtomicU64; 4],

    // Per-tier query hit/miss and predicted blocks/tokens: [tier: 4]
    tier_hits: [AtomicU64; 4],
    tier_misses: [AtomicU64; 4],
    predicted_hit_blocks: [AtomicU64; 4],
    predicted_hit_tokens: [AtomicU64; 4],
    query_tokens_total: AtomicU64,
    coverage_ratio_hist: AtomicHistogram<7>,
}

impl Default for ConductorMetrics {
    fn default() -> Self {
        Self::new()
    }
}

impl ConductorMetrics {
    pub fn new() -> Self {
        Self {
            queries: std::array::from_fn(|_| zeros()),
            query_errors: std::array::from_fn(|_| zeros()),
            query_duration: std::array::from_fn(|_| AtomicHistogram::new(LATENCY_BUCKETS)),
            query_stage_duration: std::array::from_fn(|_| {
                std::array::from_fn(|_| AtomicHistogram::new(LATENCY_BUCKETS))
            }),
            events: std::array::from_fn(|_| zeros()),
            event_batches: std::array::from_fn(|_| zeros()),
            zmq_reconnects: zeros(),
            seq_anomalies: std::array::from_fn(|_| zeros()),
            seq_gap_messages: zeros(),
            tier_hits: zeros(),
            tier_misses: zeros(),
            predicted_hit_blocks: zeros(),
            predicted_hit_tokens: zeros(),
            query_tokens_total: AtomicU64::new(0),
            coverage_ratio_hist: AtomicHistogram::new(RATIO_BUCKETS),
        }
    }

    pub fn record_query_result(
        &self,
        endpoint: QueryEndpoint,
        result: QueryResultKind,
        total_secs: f64,
    ) {
        self.queries[endpoint as usize][result as usize].fetch_add(1, Ordering::Relaxed);
        self.query_duration[endpoint as usize].observe(total_secs);
    }

    pub fn record_query_error(&self, endpoint: QueryEndpoint, reason: QueryErrorKind) {
        self.query_errors[endpoint as usize][reason as usize].fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_query_observation(&self, endpoint: QueryEndpoint, obs: &QueryObservation) {
        let stages = &self.query_stage_duration[endpoint as usize];
        stages[QueryStage::Hash as usize].observe(obs.hash_secs);
        stages[QueryStage::Match as usize].observe(obs.match_secs);
        stages[QueryStage::Build as usize].observe(obs.build_secs);

        self.record_tier_hit(MetricTier::Hbm, obs.best_hbm_blocks, obs.best_hbm_tokens);
        self.record_tier_hit(MetricTier::Cpu, obs.best_cpu_blocks, obs.best_cpu_tokens);
        self.record_tier_hit(MetricTier::Disk, obs.best_disk_blocks, obs.best_disk_tokens);
        self.record_tier_hit(MetricTier::Any, obs.best_any_blocks, obs.best_any_tokens);

        if obs.query_tokens > 0 {
            self.query_tokens_total
                .fetch_add(obs.query_tokens, Ordering::Relaxed);
            let ratio = (obs.best_any_tokens as f64 / obs.query_tokens as f64).clamp(0.0, 1.0);
            self.coverage_ratio_hist.observe(ratio);
        }
    }

    fn record_tier_hit(&self, tier: MetricTier, blocks: u64, tokens: u64) {
        let idx = tier as usize;
        if blocks > 0 {
            self.tier_hits[idx].fetch_add(1, Ordering::Relaxed);
            self.predicted_hit_blocks[idx].fetch_add(blocks, Ordering::Relaxed);
            self.predicted_hit_tokens[idx].fetch_add(tokens, Ordering::Relaxed);
        } else {
            self.tier_misses[idx].fetch_add(1, Ordering::Relaxed);
        }
    }

    pub fn record_event(&self, source: MetricEventSource, status: MetricEventStatus, delta: u64) {
        if delta == 0 {
            return;
        }
        self.events[source as usize][status as usize].fetch_add(delta, Ordering::Relaxed);
    }

    pub fn record_event_batch(&self, source: MetricEventSource, ok: bool) {
        let idx = if ok { 0 } else { 1 };
        self.event_batches[source as usize][idx].fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_zmq_reconnect(&self, source: MetricEventSource) {
        self.zmq_reconnects[source as usize].fetch_add(1, Ordering::Relaxed);
    }

    /// Observe a sequence number from a sequence-numbered event stream.
    /// Does not alter message delivery; only updates gap/duplicate/out_of_order counters.
    pub fn observe_sequence(
        &self,
        source: MetricEventSource,
        last_seq: &mut Option<u64>,
        seq: u64,
    ) {
        let anomalies = &self.seq_anomalies[source as usize];
        match *last_seq {
            None => {
                *last_seq = Some(seq);
            }
            Some(prev) => {
                let expected = prev.saturating_add(1);
                if seq == expected {
                    *last_seq = Some(seq);
                } else if seq > expected {
                    anomalies[SeqAnomalyKind::Gap as usize].fetch_add(1, Ordering::Relaxed);
                    self.seq_gap_messages[source as usize]
                        .fetch_add(seq - expected, Ordering::Relaxed);
                    *last_seq = Some(seq);
                } else if seq == prev {
                    anomalies[SeqAnomalyKind::Duplicate as usize].fetch_add(1, Ordering::Relaxed);
                } else {
                    anomalies[SeqAnomalyKind::OutOfOrder as usize].fetch_add(1, Ordering::Relaxed);
                }
            }
        }
    }

    /// Render all KV Conductor metrics in Prometheus text exposition format 0.0.4.
    pub fn render(&self, gauges: &ConductorGaugeSnapshot) -> String {
        let mut out = String::with_capacity(8192);
        self.render_queries(&mut out);
        Self::render_gauges(&mut out, gauges);
        self.render_events(&mut out);
        self.render_tiers(&mut out);
        out
    }

    fn render_queries(&self, out: &mut String) {
        let name = "kv_conductor_queries_total";
        write_header(
            out,
            name,
            "counter",
            "Total number of KV Conductor queries by endpoint and result.",
        );
        for ep in QueryEndpoint::ALL {
            for res in QueryResultKind::ALL {
                let labels = format!("endpoint=\"{}\",result=\"{}\"", ep.as_str(), res.as_str());
                write_sample(
                    out,
                    name,
                    &labels,
                    load(&self.queries[ep as usize][res as usize]),
                );
            }
        }

        let name = "kv_conductor_query_errors_total";
        write_header(
            out,
            name,
            "counter",
            "Total number of KV Conductor query errors by endpoint and reason.",
        );
        for ep in QueryEndpoint::ALL {
            for reason in QueryErrorKind::ALL {
                let labels = format!(
                    "endpoint=\"{}\",reason=\"{}\"",
                    ep.as_str(),
                    reason.as_str()
                );
                write_sample(
                    out,
                    name,
                    &labels,
                    load(&self.query_errors[ep as usize][reason as usize]),
                );
            }
        }

        let name = "kv_conductor_query_duration_seconds";
        write_header(
            out,
            name,
            "histogram",
            "End-to-end KV Conductor query latency in seconds.",
        );
        for ep in QueryEndpoint::ALL {
            let labels = format!("endpoint=\"{}\"", ep.as_str());
            self.query_duration[ep as usize].render_samples(out, name, &labels);
        }

        let name = "kv_conductor_query_stage_duration_seconds";
        write_header(
            out,
            name,
            "histogram",
            "Latency of KV Conductor query stages (hash, match, build) in seconds.",
        );
        for ep in QueryEndpoint::ALL {
            for stage in QueryStage::ALL {
                let labels = format!("endpoint=\"{}\",stage=\"{}\"", ep.as_str(), stage.as_str());
                self.query_stage_duration[ep as usize][stage as usize]
                    .render_samples(out, name, &labels);
            }
        }
    }

    fn render_gauges(out: &mut String, gauges: &ConductorGaugeSnapshot) {
        for (name, help, value) in [
            (
                "kv_conductor_registered_workers",
                "Number of registered worker endpoints.",
                gauges.registered_workers,
            ),
            (
                "kv_conductor_effective_workers",
                "Number of active/effective workers contributing to KV index.",
                gauges.effective_workers,
            ),
            (
                "kv_conductor_index_entries",
                "Number of (model, tenant) index entries.",
                gauges.index_entries,
            ),
        ] {
            write_header(out, name, "gauge", help);
            write_sample(out, name, "", value);
        }

        let name = "kv_conductor_index_blocks";
        write_header(
            out,
            name,
            "gauge",
            "Number of indexed KV blocks by storage tier.",
        );
        let total_blocks = gauges.hbm_blocks + gauges.cpu_blocks + gauges.disk_blocks;
        for (tier, value) in [
            ("hbm", gauges.hbm_blocks),
            ("cpu", gauges.cpu_blocks),
            ("disk", gauges.disk_blocks),
            ("total", total_blocks),
        ] {
            write_sample(out, name, &format!("tier=\"{tier}\""), value);
        }

        let name = "kv_conductor_index_offload_cache_entries";
        write_header(
            out,
            name,
            "gauge",
            "Number of entries in two-phase offload/pool matching caches.",
        );
        for (kind, value) in [
            ("offload", gauges.offload_entries),
            ("content", gauges.content_entries),
            ("pending_pool", gauges.pending_pool_entries),
        ] {
            write_sample(out, name, &format!("kind=\"{kind}\""), value);
        }

        let name = "kv_conductor_index_valid";
        write_header(
            out,
            name,
            "gauge",
            "Whether KV Conductor currently holds a valid non-empty index (1=valid, 0=invalid).",
        );
        write_sample(out, name, "", u8::from(gauges.index_valid));
    }

    fn render_events(&self, out: &mut String) {
        let name = "kv_conductor_events_total";
        write_header(
            out,
            name,
            "counter",
            "Total KV events processed by source and status.",
        );
        for src in MetricEventSource::ALL {
            for st in MetricEventStatus::ALL {
                let labels = format!("source=\"{}\",status=\"{}\"", src.as_str(), st.as_str());
                write_sample(
                    out,
                    name,
                    &labels,
                    load(&self.events[src as usize][st as usize]),
                );
            }
        }

        let name = "kv_conductor_event_batches_total";
        write_header(
            out,
            name,
            "counter",
            "Total KV event batches processed by source and status.",
        );
        for src in MetricEventSource::ALL {
            for (idx, status) in ["applied", "error"].into_iter().enumerate() {
                let labels = format!("source=\"{}\",status=\"{status}\"", src.as_str());
                write_sample(
                    out,
                    name,
                    &labels,
                    load(&self.event_batches[src as usize][idx]),
                );
            }
        }

        let name = "kv_conductor_zmq_reconnects_total";
        write_header(
            out,
            name,
            "counter",
            "Total ZMQ subscriber reconnect attempts by source.",
        );
        for src in [MetricEventSource::ZmqEngine, MetricEventSource::ZmqPool] {
            let labels = format!("source=\"{}\"", src.as_str());
            write_sample(out, name, &labels, load(&self.zmq_reconnects[src as usize]));
        }

        let name = "kv_conductor_event_seq_anomalies_total";
        write_header(
            out,
            name,
            "counter",
            "Sequence anomalies (gap, duplicate, out_of_order) observed on sequence-numbered event sources.",
        );
        for src in SEQ_SOURCES {
            for kind in SeqAnomalyKind::ALL {
                let labels = format!("source=\"{}\",kind=\"{}\"", src.as_str(), kind.as_str());
                write_sample(
                    out,
                    name,
                    &labels,
                    load(&self.seq_anomalies[src as usize][kind as usize]),
                );
            }
        }

        let name = "kv_conductor_event_seq_gap_messages_total";
        write_header(
            out,
            name,
            "counter",
            "Estimated missing event messages inferred from sequence gaps.",
        );
        for src in SEQ_SOURCES {
            let labels = format!("source=\"{}\"", src.as_str());
            write_sample(
                out,
                name,
                &labels,
                load(&self.seq_gap_messages[src as usize]),
            );
        }
    }

    fn render_tiers(&self, out: &mut String) {
        let name = "kv_conductor_tier_queries_total";
        write_header(
            out,
            name,
            "counter",
            "Query hit and miss counts per storage tier.",
        );
        for tier in MetricTier::ALL {
            let idx = tier as usize;
            for (result, value) in [
                ("hit", load(&self.tier_hits[idx])),
                ("miss", load(&self.tier_misses[idx])),
            ] {
                let labels = format!("tier=\"{}\",result=\"{result}\"", tier.as_str());
                write_sample(out, name, &labels, value);
            }
        }

        let name = "kv_conductor_tier_hit_rate";
        write_header(
            out,
            name,
            "gauge",
            "Cumulative query hit rate per storage tier.",
        );
        for tier in MetricTier::ALL {
            let hits = load(&self.tier_hits[tier as usize]);
            let total = hits + load(&self.tier_misses[tier as usize]);
            let rate = if total > 0 {
                hits as f64 / total as f64
            } else {
                0.0
            };
            let labels = format!("tier=\"{}\"", tier.as_str());
            write_sample(out, name, &labels, format!("{rate:.6}"));
        }

        for (name, help, values) in [
            (
                "kv_conductor_predicted_hit_blocks_total",
                "Cumulative predicted matched blocks per storage tier.",
                &self.predicted_hit_blocks,
            ),
            (
                "kv_conductor_predicted_hit_tokens_total",
                "Cumulative predicted matched tokens per storage tier.",
                &self.predicted_hit_tokens,
            ),
        ] {
            write_header(out, name, "counter", help);
            for tier in MetricTier::ALL {
                let labels = format!("tier=\"{}\"", tier.as_str());
                write_sample(out, name, &labels, load(&values[tier as usize]));
            }
        }

        let queried_tokens = load(&self.query_tokens_total);
        let name = "kv_conductor_query_tokens_total";
        write_header(out, name, "counter", "Cumulative queried prompt tokens.");
        write_sample(out, name, "", queried_tokens);

        let predicted_any_tokens = load(&self.predicted_hit_tokens[MetricTier::Any as usize]);
        let avg_coverage = if queried_tokens > 0 {
            (predicted_any_tokens as f64 / queried_tokens as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let name = "kv_conductor_predicted_token_coverage_ratio";
        write_header(
            out,
            name,
            "gauge",
            "Cumulative predicted matched token coverage ratio.",
        );
        write_sample(out, name, "", format!("{avg_coverage:.6}"));

        let name = "kv_conductor_predicted_token_coverage_ratio_dist";
        write_header(
            out,
            name,
            "histogram",
            "Distribution of per-query predicted token coverage ratio.",
        );
        self.coverage_ratio_hist.render_samples(out, name, "");
    }
}
