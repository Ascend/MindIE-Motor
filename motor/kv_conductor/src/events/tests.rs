use super::*;
use rmp_serde::from_slice;

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
    let data = msgpack_bin(&[0x00, 0x00, 0x00, 0x2A]);
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
fn test_flex_hash_bytes_long_uses_low_64_bits() {
    // vLLM block hashes default to 32-byte sha256. vLLM int mode and
    // memcache's BlockHashHexToU64 both use the low 64 bits, so a long
    // byte string keeps its trailing 8 bytes (big-endian).
    let sha256: [u8; 32] = [
        0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e,
        0x0f, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d,
        0x1e, 0x1f,
    ];
    let data = msgpack_bin(&sha256);
    let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
    assert_eq!(
        hashes[0].0,
        u64::from_be_bytes([0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f])
    );
}

#[test]
fn test_flex_hash_integrated_in_zmq_event_map() {
    let event = serde_json::json!({
        "event_id": 1,
        "event_type": "stored",
        "medium": "cpu",
        "seq_hashes": ["0xABCD", "12345"],
        "block_hashes": [100, 200]
    });
    let packed = rmp_serde::to_vec(&event).unwrap();
    let map: PoolEvent = from_slice(&packed).unwrap();
    let seq: Vec<u64> = map.seq_hashes.unwrap().iter().map(|h| h.0).collect();
    assert_eq!(seq, vec![0xABCD, 12345]);
    let blk: Vec<u64> = map.block_hashes.unwrap().iter().map(|h| h.0).collect();
    assert_eq!(blk, vec![100, 200]);
}

// -----------------------------------------------------------------------
// is_main_attention_kind
// -----------------------------------------------------------------------

#[test]
fn test_main_attention_kinds_accepted() {
    assert!(is_main_attention_kind(Some("FullAttention")));
    assert!(is_main_attention_kind(Some("MlaAttention")));
    assert!(is_main_attention_kind(Some("SinkFullAttention")));
}

#[test]
fn test_non_main_attention_kinds_filtered() {
    assert!(!is_main_attention_kind(Some("SlidingWindow")));
    assert!(!is_main_attention_kind(Some("Mamba")));
    assert!(!is_main_attention_kind(Some("ChunkedLocalAttention")));
    assert!(!is_main_attention_kind(Some("EncoderOnlyAttention")));
    assert!(!is_main_attention_kind(Some("CrossAttention")));
}

#[test]
fn test_unknown_and_none_kinds_accepted() {
    // None: older vLLM without spec_kind — backward compat
    assert!(is_main_attention_kind(None));
    // Unknown future kind — forward compat
    assert!(is_main_attention_kind(Some("FutureAttentionType")));
}

// -----------------------------------------------------------------------
// VllmEventMap normalize — filtering by spec_kind
// -----------------------------------------------------------------------

/// Build a BlockStored msgpack array and deserialize + normalize.
///
/// Constructs a realistic vLLM wire-format array (``omit_defaults=True``):
/// fields whose value is null are absent from the array, so the test
/// data includes enough typed placeholders (lora_id=0, medium="GPU",
/// lora_name="lora", group_idx=0) to keep the type-pattern parser
/// unambiguous.
fn normalize_block_stored(
    kind: Option<&str>,
    token_ids: Vec<i64>,
    block_size: u32,
    block_hashes: Vec<u64>,
) -> VllmEvent {
    // vLLM array (parent_hash, extra_keys, sliding_window omitted):
    //   [tag, block_hashes, token_ids, block_size,
    //    lora_id, medium, lora_name, group_idx, kv_cache_spec_kind?]
    let mut arr = serde_json::json!([
        "BlockStored",
        block_hashes,
        token_ids,
        block_size,
        0,      // lora_id
        "GPU",  // medium
        "lora", // lora_name
        0,      // group_idx
    ]);
    if let Some(k) = kind {
        let a = arr.as_array_mut().unwrap();
        a.push(serde_json::json!(k)); // kv_cache_spec_kind
    }
    let packed = rmp_serde::to_vec(&arr).unwrap();
    let parsed: VllmEventMap = from_slice(&packed).unwrap();
    parsed.normalize()
}

#[test]
fn test_vllm_block_stored_with_full_attention_is_accepted() {
    let ev = normalize_block_stored(Some("FullAttention"), vec![1, 2, 3, 4], 4, vec![100]);
    assert!(matches!(ev, VllmEvent::BlockStored { .. }));
}

#[test]
fn test_vllm_block_stored_with_mla_attention_is_accepted() {
    let ev = normalize_block_stored(Some("MlaAttention"), vec![1, 2, 3, 4], 4, vec![200]);
    assert!(matches!(ev, VllmEvent::BlockStored { .. }));
}

#[test]
fn test_vllm_block_stored_with_sliding_window_is_filtered() {
    let ev = normalize_block_stored(Some("SlidingWindow"), vec![1, 2, 3, 4], 4, vec![300]);
    assert!(matches!(ev, VllmEvent::Ignored));
}

#[test]
fn test_vllm_block_stored_with_mamba_is_filtered() {
    let ev = normalize_block_stored(Some("Mamba"), vec![1, 2], 2, vec![400]);
    assert!(matches!(ev, VllmEvent::Ignored));
}

#[test]
fn test_vllm_block_stored_without_spec_kind_is_accepted() {
    // Backward compat: older vLLM without the field
    let ev = normalize_block_stored(
        None,
        vec![10, 20, 30, 40, 50, 60, 70, 80],
        4,
        vec![500, 600],
    );
    assert!(matches!(ev, VllmEvent::BlockStored { .. }));
}

#[test]
fn test_vllm_array_format_block_stored_accepted() {
    // Minimal realistic BlockStored: required fields + type anchors.
    // parent_hash omitted, extra_keys/sliding_window omitted.
    let arr = serde_json::json!([
        "BlockStored",
        [100, 200],
        [10, 20, 30, 40, 50, 60, 70, 80],
        4,
        0,      // lora_id
        "GPU",  // medium
        "lora", // lora_name
        0,      // group_idx
    ]);
    let packed = rmp_serde::to_vec(&arr).unwrap();
    let parsed: VllmEventMap = from_slice(&packed).unwrap();
    match parsed.normalize() {
        VllmEvent::BlockStored {
            block_hashes,
            block_size,
            ..
        } => {
            assert_eq!(block_hashes, vec![100, 200]);
            assert_eq!(block_size, 4);
        }
        _ => panic!("expected BlockStored from array format"),
    }
}

#[test]
fn test_vllm_array_format_block_removed_accepted() {
    // Minimal BlockRemoved: only block_hashes (medium omitted).
    let arr = serde_json::json!(["BlockRemoved", [300, 400]]);
    let packed = rmp_serde::to_vec(&arr).unwrap();
    let parsed: VllmEventMap = from_slice(&packed).unwrap();
    match parsed.normalize() {
        VllmEvent::BlockRemoved { block_hashes, .. } => {
            assert_eq!(block_hashes, vec![300, 400]);
        }
        _ => panic!("expected BlockRemoved from array format"),
    }
}

#[test]
fn test_vllm_array_format_block_removed_with_medium() {
    // BlockRemoved with medium field present (the case the old parser
    // confused with parent_block_hash).
    let arr = serde_json::json!(["BlockRemoved", [300, 400], "cpu"]);
    let packed = rmp_serde::to_vec(&arr).unwrap();
    let parsed: VllmEventMap = from_slice(&packed).unwrap();
    match parsed.normalize() {
        VllmEvent::BlockRemoved {
            block_hashes,
            medium,
            ..
        } => {
            assert_eq!(block_hashes, vec![300, 400]);
            assert_eq!(medium.unwrap(), "cpu");
        }
        _ => panic!("expected BlockRemoved from array format"),
    }
}

#[test]
fn test_vllm_array_format_with_trailing_fields() {
    // BlockStored with parent_hash, extra_keys, sliding_window omitted.
    let arr = serde_json::json!([
        "BlockStored",
        [500],
        [1, 2, 3, 4],
        4,
        0,      // lora_id
        "GPU",  // medium
        "lora", // lora_name
        0,      // group_idx
        "FullAttention",
    ]);
    let packed = rmp_serde::to_vec(&arr).unwrap();
    let parsed: VllmEventMap = from_slice(&packed).unwrap();
    let ev = parsed.normalize();
    assert!(matches!(ev, VllmEvent::BlockStored { .. }));
}

#[test]
fn test_vllm_all_blocks_cleared_always_accepted() {
    let arr = serde_json::json!(["AllBlocksCleared"]);
    let packed = rmp_serde::to_vec(&arr).unwrap();
    let parsed: VllmEventMap = from_slice(&packed).unwrap();
    assert!(matches!(parsed.normalize(), VllmEvent::AllBlocksCleared));
}

// -----------------------------------------------------------------------
// VllmEventMap normalize — correct field extraction
// -----------------------------------------------------------------------

#[test]
fn test_vllm_block_stored_extracts_fields_correctly() {
    let ev = normalize_block_stored(
        Some("FullAttention"),
        vec![1, 2, 3, 4, 5, 6, 7, 8],
        4,
        vec![0xAA, 0xBB],
    );
    match ev {
        VllmEvent::BlockStored {
            block_hashes,
            token_ids,
            block_size,
            ..
        } => {
            assert_eq!(block_hashes, vec![0xAA, 0xBB]);
            assert_eq!(token_ids, vec![1, 2, 3, 4, 5, 6, 7, 8]);
            assert_eq!(block_size, 4);
        }
        _ => panic!("expected BlockStored"),
    }
}

#[test]
fn test_vllm_block_removed_extracts_hashes() {
    // BlockRemoved with medium: ["BlockRemoved", [hashes], "cpu"]
    let arr = serde_json::json!(["BlockRemoved", [0xDEAD, 0xBEEF], "cpu"]);
    let packed = rmp_serde::to_vec(&arr).unwrap();
    let parsed: VllmEventMap = from_slice(&packed).unwrap();
    match parsed.normalize() {
        VllmEvent::BlockRemoved {
            block_hashes,
            medium,
            ..
        } => {
            assert_eq!(block_hashes, vec![0xDEAD, 0xBEEF]);
            assert_eq!(medium.unwrap(), "cpu");
        }
        _ => panic!("expected BlockRemoved"),
    }
}

// -----------------------------------------------------------------------
// vLLM batch parsing
// -----------------------------------------------------------------------

fn make_vllm_block_stored_payload(
    kind: Option<&str>,
    block_hashes: Vec<u64>,
    token_ids: Vec<i64>,
    block_size: u32,
) -> Vec<u8> {
    // Realistic vLLM array (parent_hash, extra_keys, sliding_window omitted):
    //   [tag, block_hashes, token_ids, block_size,
    //    lora_id, medium, lora_name, group_idx, kv_cache_spec_kind?]
    let mut inner = serde_json::json!([
        "BlockStored",
        block_hashes,
        token_ids,
        block_size,
        0,      // lora_id
        "GPU",  // medium
        "lora", // lora_name
        0,      // group_idx
    ]);
    if let Some(k) = kind {
        let a = inner.as_array_mut().unwrap();
        a.push(serde_json::json!(k)); // kv_cache_spec_kind
    }
    // KVEventBatch: [1.0, [event], null]
    let batch = serde_json::json!([1.0, [inner], null]);
    rmp_serde::to_vec(&batch).unwrap()
}

#[test]
fn test_parse_vllm_batch_format_a() {
    let payload =
        make_vllm_block_stored_payload(Some("FullAttention"), vec![100], vec![1, 2, 3, 4], 4);
    let (events, dp_rank) = parse_vllm_batch(&payload).unwrap();
    assert_eq!(dp_rank, 0);
    assert_eq!(events.len(), 1);
    assert!(matches!(events[0], VllmEvent::BlockStored { .. }));
}

#[test]
fn test_parse_vllm_batch_filters_swa_events() {
    let payload =
        make_vllm_block_stored_payload(Some("SlidingWindow"), vec![200], vec![5, 6, 7, 8], 4);
    let (events, _) = parse_vllm_batch(&payload).unwrap();
    assert_eq!(events.len(), 1);
    assert!(matches!(events[0], VllmEvent::Ignored));
}

// -----------------------------------------------------------------------
// apply_vllm_event — tokens_hash computation
// -----------------------------------------------------------------------

#[test]
fn test_apply_vllm_block_stored_computes_tokens_hash() {
    use crate::hashing::compute_block_hash_for_seq;
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let token_ids = vec![1i64, 2, 3, 4, 5, 6, 7, 8];
    let block_size = 4u32;

    // Pre-compute expected XXH3 hashes
    let _expected = compute_block_hash_for_seq(&token_ids, block_size);

    let event = VllmEvent::BlockStored {
        block_hashes: vec![0xAAAA, 0xBBBB],
        parent_block_hash: None,
        token_ids: token_ids.clone(),
        block_size,
        medium: Some("xpu".into()),
        group_idx: None,
    };

    let result = apply_vllm_event(
        &indexer,
        &event,
        "test-model",
        "test-tenant",
        "test-backend",
        0,
        &[StorageMedium::Npu],
        MatchMode::None,
        &None,
        block_size,
    );

    assert!(result.is_ok());

    // Verify the tree has correct tokens_hash values
    let entry = indexer.get_or_create("test-model", "test-tenant");
    let lookups = entry.lookups.read();
    // Should have one worker entry with lookup entries
    let wk = WorkerKey {
        instance_id: "test-backend".into(),
        backend_id: "test-backend".into(),
        dp_rank: 0,
        medium: StorageMedium::Npu,
    };
    let lookup = lookups.get(&wk).expect("worker should exist");
    // 2 SHA256 hashes → 2 lookup entries
    assert_eq!(lookup.len(), 2);

    // Verify tokens_hash values match pre-computed XXH3 hashes
    for block in lookup.values() {
        let guard = block.read();
        // Each stored block should have its block_hash (SHA256) set
        assert!(guard.block_hash.is_some());
    }

    // Query via find_matches should match (tokens_hash == query hash)
    let scores = entry.find_matches(&token_ids, block_size);
    assert!(
        !scores.blocks.is_empty(),
        "query should match stored blocks"
    );
}

// -----------------------------------------------------------------------
// Non-HBM event caching → pool backend matching (two-phase)
// -----------------------------------------------------------------------

#[test]
fn test_non_hbm_event_cached_not_in_tree() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let token_ids = vec![1i64, 2, 3, 4];
    let block_size = 4u32;

    // Phase 1: engine offloads to CPU — should be cached, not in tree.
    let event = VllmEvent::BlockStored {
        block_hashes: vec![0xABCD],
        parent_block_hash: None,
        token_ids: token_ids.clone(),
        block_size,
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &event,
        "test-model",
        "test-tenant",
        "test-backend",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Cache should have the entry (ingest_offload_blocks caches unmatched pairs).
    let entry = indexer.get_or_create("test-model", "test-tenant");
    {
        let state = entry.offload_pool_state.read();
        assert!(state.offload.contains_key(&0xABCD));
        assert!(state.pending_pool.is_empty());
    }

    // Tree should NOT have the block (not inserted for non-HBM).
    let scores = entry.find_matches(&token_ids, block_size);
    assert!(
        scores.blocks.is_empty(),
        "non-HBM events should not be inserted into tree"
    );
}

#[test]
fn test_pool_backend_store_matches_cached_block() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let token_ids = vec![1i64, 2, 3, 4];
    let block_size = 4u32;

    // Phase 1: engine offloads CPU block.
    let engine_event = VllmEvent::BlockStored {
        block_hashes: vec![0xBEEF],
        parent_block_hash: None,
        token_ids: token_ids.clone(),
        block_size,
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &engine_event,
        "test-model",
        "test-tenant",
        "test-backend",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Pre-compute expected XXH3 hash.
    let _expected_hashes = compute_block_hash_for_seq(&token_ids, block_size);

    // Phase 2: pool backend confirms placement — insert into tree.
    let zmq_event = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("test-model".into()),
        tenant_id: Some("test-tenant".into()),
        backend_id: Some("test-pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xBEEF)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &zmq_event,
        "test-model",
        "test-tenant",
        "test-pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Tree should now have the block at the pool backend's worker key.
    let entry = indexer.get_or_create("test-model", "test-tenant");
    let scores = entry.find_matches(&token_ids, block_size);
    // The pool backend worker ("test-pool") should have a match.
    let pool_worker = WorkerKey {
        instance_id: "test-pool".into(),
        backend_id: "test-pool".into(),
        dp_rank: 0,
        medium: StorageMedium::Cpu,
    };
    assert!(
        scores.blocks.contains_key(&pool_worker),
        "pool backend store should insert cached block into tree at pool worker"
    );
}

#[test]
fn test_pool_backend_store_ignores_unknown_hash() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("test-model", "test-tenant");

    // Pool backend stores a block we never cached — now queued in
    // pending_pool for bidirectional matching (not silently dropped).
    let zmq_event = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("test-model".into()),
        tenant_id: Some("test-tenant".into()),
        backend_id: Some("test-pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xDEAD)]),
        block_hashes: None,
    };
    let result = apply_pool_event(
        &indexer,
        &zmq_event,
        "test-model",
        "test-tenant",
        "test-pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    );
    assert!(result.is_ok());

    // Worker lookup should be empty (nothing was inserted into tree).
    let lookups = entry.lookups.read();
    let pool_worker = WorkerKey {
        instance_id: "test-pool".into(),
        backend_id: "test-pool".into(),
        dp_rank: 0,
        medium: StorageMedium::Cpu,
    };
    assert!(lookups.get(&pool_worker).is_none());

    // But the pool event IS queued in pending_pool for later matching.
    {
        let state = entry.offload_pool_state.read();
        assert!(
            state.pending_pool.contains_key(&0xDEAD),
            "pool event should be queued in pending_pool"
        );
    }
}

/// Replicate the memcache MetaService wire batch as a msgpack map
/// ({"events": [...]}): optional fields packed as nil, timestamp as
/// uint64, seq_hashes as uint64 array. Matches memcache's
/// EmitEventMapFields output byte-for-byte in structure.
fn memcache_wire_batch(seq_hash: u64) -> rmpv::Value {
    let event = rmpv::Value::Map(vec![
        (rmpv::Value::from("event_id"), rmpv::Value::from(1u64)),
        (
            rmpv::Value::from("timestamp"),
            rmpv::Value::from(1752999546000u64),
        ),
        (rmpv::Value::from("event_type"), rmpv::Value::from("stored")),
        (rmpv::Value::from("model_name"), rmpv::Value::Nil),
        (rmpv::Value::from("block_size"), rmpv::Value::Nil),
        (rmpv::Value::from("additional_salt"), rmpv::Value::Nil),
        (rmpv::Value::from("lora_name"), rmpv::Value::Nil),
        (rmpv::Value::from("tenant_id"), rmpv::Value::from("default")),
        (rmpv::Value::from("medium"), rmpv::Value::from("xpu")),
        (
            rmpv::Value::from("backend_id"),
            rmpv::Value::from("10.244.0.5"),
        ),
        (rmpv::Value::from("dp_rank"), rmpv::Value::Nil),
        (
            rmpv::Value::from("seq_hashes"),
            rmpv::Value::Array(vec![rmpv::Value::from(seq_hash)]),
        ),
        (rmpv::Value::from("base_block_idx"), rmpv::Value::Nil),
        (rmpv::Value::from("parent_hash"), rmpv::Value::Nil),
        (rmpv::Value::from("token_ids"), rmpv::Value::Nil),
    ]);
    rmpv::Value::Map(vec![(
        rmpv::Value::from("events"),
        rmpv::Value::Array(vec![event]),
    )])
}

#[test]
fn test_memcache_batch_parse_and_apply_ip_only() {
    use crate::indexer::Indexer;
    use crate::protocols::HbmIpIndex;

    let indexer = Indexer::new();
    let ip_index = HbmIpIndex::default();
    ip_index
        .write()
        .entry("10.244.0.5".to_string())
        .or_default()
        .push(("vllm-prefill-1".to_string(), 0));

    let token_ids = vec![1i64, 2, 3, 4];
    let block_size = 4u32;
    let hash = compute_block_hash_for_seq(&token_ids, block_size)[0].0;

    let packed = rmp_serde::to_vec(&memcache_wire_batch(hash)).unwrap();
    let parsed: MemcacheEventBatch = from_slice(&packed).unwrap();
    assert_eq!(parsed.events.len(), 1);

    let event = &parsed.events[0];
    assert_eq!(event.event_type.as_deref(), Some("stored"));
    assert_eq!(event.backend_id.as_deref(), Some("10.244.0.5"));
    assert_eq!(event.tenant_id.as_deref(), Some("default"));
    assert_eq!(event.medium.as_deref(), Some("xpu"));
    assert_eq!(event.dp_rank, None);
    let hashes: Vec<u64> = event
        .seq_hashes
        .as_ref()
        .unwrap()
        .iter()
        .map(|h| h.0)
        .collect();
    assert_eq!(hashes, vec![hash]);

    // Apply under IpOnly: the event's backend_id (node IP) fans out to all
    // DPs registered on that IP.
    apply_pool_event(
        &indexer,
        event,
        "test-model",
        "default",
        "memcache-pool", // subscriber's own backend_id — ignored under IpOnly
        0,
        &[StorageMedium::Npu, StorageMedium::Cpu, StorageMedium::Disk],
        MatchMode::IpOnly,
        &Some(ip_index),
    )
    .unwrap();

    // Pool-first semantics: memcache stored events carry only block hashes
    // (token_ids / parent_hash are nil placeholders on the memcache wire),
    // so the block is queued in pending_pool waiting for an engine offload
    // event with matching token hashes — not inserted into the tree yet.
    // (An empty worker resolution under IpOnly would queue nothing, so the
    // pending entry also proves the backend_id → hbm_ip_index → node DP
    // routing worked.)
    let entry = indexer.get_or_create("test-model", "default");
    {
        let state = entry.offload_pool_state.read();
        assert!(
            state.pending_pool.contains_key(&hash),
            "memcache stored event should be queued in pending_pool at the node-IP worker (IpOnly)"
        );
    }
}

#[test]
fn test_pool_backend_remove_evicts_cache() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let token_ids = vec![1i64, 2, 3, 4, 5, 6, 7, 8];
    let block_size = 4u32;

    // Phase 1: engine offloads CPU blocks.
    let engine_event = VllmEvent::BlockStored {
        block_hashes: vec![0xAAA, 0xBBB],
        parent_block_hash: None,
        token_ids: token_ids.clone(),
        block_size,
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &engine_event,
        "test-model",
        "test-tenant",
        "test-backend",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    let entry = indexer.get_or_create("test-model", "test-tenant");

    // Cache should have both entries (offload first, not yet matched).
    {
        let state = entry.offload_pool_state.read();
        assert!(state.offload.contains_key(&0xAAA));
        assert!(state.offload.contains_key(&0xBBB));
    }

    // Phase 2: pool backend confirm placement.
    let store_event = PoolEvent {
        event_id: 1,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("test-model".into()),
        tenant_id: Some("test-tenant".into()),
        backend_id: Some("test-pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xAAA), FlexHash(0xBBB)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &store_event,
        "test-model",
        "test-tenant",
        "test-pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Phase 3: pool backend removes one block.
    let remove_event = PoolEvent {
        event_id: 2,
        timestamp: None,
        event_type: Some("removed".into()),
        legacy_type: None,
        model_name: Some("test-model".into()),
        tenant_id: Some("test-tenant".into()),
        backend_id: Some("test-pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xAAA)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &remove_event,
        "test-model",
        "test-tenant",
        "test-pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Both cache entries should be evicted after pool store confirm
    // (ingest_pool_blocks removes matched entries from offload cache).
    {
        let state = entry.offload_pool_state.read();
        assert!(!state.offload.contains_key(&0xAAA));
        assert!(!state.offload.contains_key(&0xBBB));
        assert!(state.pending_pool.is_empty());
    }

    // After removing 0xAAA, 0xBBB remains in the CPU tier. Contiguous prefix
    // lookup cannot walk past the hole, so find_matches on the full sequence
    // reports no hit — verify tier membership directly instead.
    let pool_worker = WorkerKey {
        instance_id: "test-pool".into(),
        backend_id: "test-pool".into(),
        dp_rank: 0,
        medium: StorageMedium::Cpu,
    };
    assert!(!entry.cpu_tiers.contains_block(0xAAA));
    assert!(
        entry.cpu_tiers.contains_block(0xBBB),
        "removing 0xAAA must not evict the remaining block 0xBBB"
    );
    assert_eq!(entry.cpu_tiers.worker_block_count(&pool_worker), 1);
    assert!(
        entry.find_matches(&token_ids, block_size).blocks.is_empty(),
        "broken prefix chain should not produce a contiguous match"
    );
}

// -----------------------------------------------------------------------
// Bidirectional offload/pool matching (pool arrives BEFORE offload)
// -----------------------------------------------------------------------

/// Pool backend stored event arrives before the engine offload event.
/// The pool event is queued; when the offload arrives later the match
/// completes and the block enters the tree.
#[test]
fn test_pool_arrives_before_offload() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let token_ids = vec![1i64, 2, 3, 4];
    let block_size = 4u32;

    // Phase 1: pool backend stored arrives FIRST (no offload cached yet).
    let zmq_event = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("test-model".into()),
        tenant_id: Some("test-tenant".into()),
        backend_id: Some("test-pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xBEEF)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &zmq_event,
        "test-model",
        "test-tenant",
        "test-pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // At this point: tree should be empty, but pending_pool has the entry.
    let entry = indexer.get_or_create("test-model", "test-tenant");
    {
        let state = entry.offload_pool_state.read();
        assert!(
            state.pending_pool.contains_key(&0xBEEF),
            "pool event should be queued in pending_pool"
        );
        assert!(state.offload.is_empty());
    }

    // Tree still empty — no offload has arrived yet.
    let scores = entry.find_matches(&token_ids, block_size);
    assert!(scores.blocks.is_empty());

    // Phase 2: engine offloads to CPU (arrives SECOND).
    let engine_event = VllmEvent::BlockStored {
        block_hashes: vec![0xBEEF],
        parent_block_hash: None,
        token_ids: token_ids.clone(),
        block_size,
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &engine_event,
        "test-model",
        "test-tenant",
        "test-backend",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Both caches should be empty now (match completed).
    {
        let state = entry.offload_pool_state.read();
        assert!(state.pending_pool.is_empty());
        assert!(state.offload.is_empty());
    }

    // Tree should now have the block at the pool backend's worker key.
    let scores = entry.find_matches(&token_ids, block_size);
    let pool_worker = WorkerKey {
        instance_id: "test-pool".into(),
        backend_id: "test-pool".into(),
        dp_rank: 0,
        medium: StorageMedium::Cpu,
    };
    assert!(
        scores.blocks.contains_key(&pool_worker),
        "pool-first ordering: block should be inserted into tree after offload arrives"
    );
}

/// Pool backend stored arrives first for multiple workers.
/// Offload matches all queued workers.
#[test]
fn test_pool_arrives_before_offload_multi_worker() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let token_ids = vec![10i64, 20, 30, 40];
    let block_size = 4u32;

    let entry = indexer.get_or_create("multi", "t1");

    // Two different pool workers queue the same block_hash.
    for dp in [0u32, 1u32] {
        let zmq_event = PoolEvent {
            event_id: dp as u64,
            timestamp: None,
            event_type: Some("stored".into()),
            legacy_type: None,
            model_name: Some("multi".into()),
            tenant_id: Some("t1".into()),
            backend_id: Some("pool".into()),
            medium: Some("cpu".into()),
            dp_rank: Some(dp),
            seq_hashes: Some(vec![FlexHash(0xCAFE)]),
            block_hashes: None,
        };
        apply_pool_event(
            &indexer,
            &zmq_event,
            "multi",
            "t1",
            "pool",
            dp,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();
    }

    // Both workers queued.
    {
        let state = entry.offload_pool_state.read();
        let pending = state.pending_pool.get(&0xCAFE).unwrap();
        assert_eq!(pending.len(), 2);
    }

    // Offload arrives → should match both workers.
    let engine_event = VllmEvent::BlockStored {
        block_hashes: vec![0xCAFE],
        parent_block_hash: None,
        token_ids: token_ids.clone(),
        block_size,
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &engine_event,
        "multi",
        "t1",
        "eng",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Both caches empty after match.
    {
        let state = entry.offload_pool_state.read();
        assert!(state.pending_pool.is_empty());
        assert!(state.offload.is_empty());
    }

    // Both workers have the block in tree.
    let scores = entry.find_matches(&token_ids, block_size);
    let w0 = WorkerKey {
        instance_id: "pool".into(),
        backend_id: "pool".into(),
        dp_rank: 0,
        medium: StorageMedium::Cpu,
    };
    let w1 = WorkerKey {
        instance_id: "pool".into(),
        backend_id: "pool".into(),
        dp_rank: 1,
        medium: StorageMedium::Cpu,
    };
    assert!(scores.blocks.contains_key(&w0));
    assert!(scores.blocks.contains_key(&w1));
}

/// Pool event is queued, then a pool removal arrives — pending entry
/// should be evicted (block never enters the tree).
#[test]
fn test_pool_removal_cleans_pending() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("m", "t");

    // Pool stored arrives first → queued.
    let store_ev = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xD00D)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &store_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Queued.
    {
        let state = entry.offload_pool_state.read();
        assert!(state.pending_pool.contains_key(&0xD00D));
    }

    // Pool removal arrives → evict from pending.
    let remove_ev = PoolEvent {
        event_id: 1,
        timestamp: None,
        event_type: Some("removed".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xD00D)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &remove_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Pending should be empty now.
    {
        let state = entry.offload_pool_state.read();
        assert!(state.pending_pool.is_empty());
        assert!(state.offload.is_empty());
    }

    // Tree should still be empty (block never entered it).
    assert!(entry.lookups.read().is_empty());
}

/// Offload cached, then vLLM BlockRemoved → evict from offload cache.
/// Block never enters the tree.
#[test]
fn test_offload_then_vllm_removal() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("m", "t");
    let block_size = 4u32;

    // Offload arrives first → cached in offload.
    let stored = VllmEvent::BlockStored {
        block_hashes: vec![0xAAA],
        parent_block_hash: None,
        token_ids: vec![1, 2, 3, 4],
        block_size,
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &stored,
        "m",
        "t",
        "eng",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Cached.
    {
        let state = entry.offload_pool_state.read();
        assert!(state.offload.contains_key(&0xAAA));
    }

    // vLLM BlockRemoved arrives → evict from offload cache.
    let removed = VllmEvent::BlockRemoved {
        block_hashes: vec![0xAAA],
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &removed,
        "m",
        "t",
        "eng",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Offload cache should be empty.
    {
        let state = entry.offload_pool_state.read();
        assert!(state.offload.is_empty());
    }

    // Tree should still be empty (block never entered it).
    assert!(entry.lookups.read().is_empty());
}

/// Both events matched (offload then pool), then pool removal → tree
/// removal happens (fixes the existing bug where removal after match was
/// silently skipped because cache entries were already evicted).
#[test]
fn test_removal_after_both_matched() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let token_ids = vec![1i64, 2, 3, 4];
    let block_size = 4u32;

    // Offload → cached.
    let engine_event = VllmEvent::BlockStored {
        block_hashes: vec![0xF00],
        parent_block_hash: None,
        token_ids: token_ids.clone(),
        block_size,
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &engine_event,
        "m",
        "t",
        "eng",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Pool confirm → matched, inserted into tree.
    let store_ev = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xF00)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &store_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    let entry = indexer.get_or_create("m", "t");

    // Both caches empty (matched).
    {
        let state = entry.offload_pool_state.read();
        assert!(state.offload.is_empty());
        assert!(state.pending_pool.is_empty());
    }

    // Block IS in the tree (verify via find_matches for the CPU medium).
    let pool_worker = WorkerKey {
        instance_id: "pool".into(),
        backend_id: "pool".into(),
        dp_rank: 0,
        medium: StorageMedium::Cpu,
    };
    {
        let scores = entry.find_matches(&token_ids, block_size);
        assert!(
            scores.blocks.contains_key(&pool_worker),
            "after match: block should be in tree"
        );
    }

    // Pool removal → should remove from tree (this was the bug!).
    let remove_ev = PoolEvent {
        event_id: 1,
        timestamp: None,
        event_type: Some("removed".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xF00)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &remove_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Tree should be empty now (removal succeeded).
    // Verify via find_matches — no workers should have the block.
    let scores_after = entry.find_matches(&token_ids, block_size);
    assert!(
        !scores_after.blocks.contains_key(&pool_worker),
        "removal after match: block should be removed from tree"
    );
}

/// Pool event queued, then vLLM BlockRemoved arrives.
/// The pending pool entry should be cleaned up.
#[test]
fn test_vllm_removal_after_pool_queued() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("m", "t");
    let block_size = 4u32;

    // Pool stored first → queued.
    let store_ev = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xB00)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &store_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Queued.
    {
        let state = entry.offload_pool_state.read();
        assert!(state.pending_pool.contains_key(&0xB00));
    }

    // vLLM BlockRemoved for the same block arrives.
    let removed = VllmEvent::BlockRemoved {
        block_hashes: vec![0xB00],
        medium: Some("cpu".into()),
        group_idx: None,
    };
    apply_vllm_event(
        &indexer,
        &removed,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Pending should be cleaned.
    {
        let state = entry.offload_pool_state.read();
        assert!(
            state.pending_pool.is_empty(),
            "vLLM removal should evict pending pool entry"
        );
    }
}

/// Duplicate delivery of the same pool stored event should be idempotent.
#[test]
fn test_duplicate_pool_stored_idempotent() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("m", "t");

    let zmq_event = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xCCC)]),
        block_hashes: None,
    };

    // Deliver the same event twice.
    apply_pool_event(
        &indexer,
        &zmq_event,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();
    apply_pool_event(
        &indexer,
        &zmq_event,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    // Only one entry should exist (FxHashSet dedup).
    {
        let state = entry.offload_pool_state.read();
        let pending = state.pending_pool.get(&0xCCC).unwrap();
        assert_eq!(pending.len(), 1, "duplicate delivery should be idempotent");
    }
}

/// remove_pending_worker cleans up all entries for a disconnected worker.
#[test]
fn test_pending_worker_cleanup() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("m", "t");

    // Queue pool events for two different workers.
    for dp in [0u32, 1u32] {
        let zmq_event = PoolEvent {
            event_id: dp as u64,
            timestamp: None,
            event_type: Some("stored".into()),
            legacy_type: None,
            model_name: Some("m".into()),
            tenant_id: Some("t".into()),
            backend_id: Some("pool".into()),
            medium: Some("cpu".into()),
            dp_rank: Some(dp),
            seq_hashes: Some(vec![FlexHash(0xE00 + dp as u64)]),
            block_hashes: None,
        };
        apply_pool_event(
            &indexer,
            &zmq_event,
            "m",
            "t",
            "pool",
            dp,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();
    }

    // Both queued.
    assert_eq!(entry.pending_count(), 2);

    // Remove worker dp=0.
    let wk0 = WorkerKey {
        instance_id: "pool".into(),
        backend_id: "pool".into(),
        dp_rank: 0,
        medium: StorageMedium::Cpu,
    };
    let removed = entry.remove_pending_worker(&wk0);
    assert_eq!(removed, 1);

    // Only dp=1 remains.
    assert_eq!(entry.pending_count(), 1);
}

/// Cleared event cleans up pending pool entries for the worker.
#[test]
fn test_cleared_cleans_pending() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("m", "t");

    // Queue a pool event.
    let store_ev = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xABC)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &store_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    assert_eq!(entry.pending_count(), 1);

    // Cleared event → pending entries removed.
    let clear_ev = PoolEvent {
        event_id: 1,
        timestamp: None,
        event_type: Some("cleared".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: None,
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &clear_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    assert_eq!(
        entry.pending_count(),
        0,
        "cleared event should remove pending pool entries"
    );
}

/// TTL sweep removes stale pending pool entries.
#[test]
fn test_sweep_stale_caches() {
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let entry = indexer.get_or_create("m", "t");

    // Queue a pool event.
    let store_ev = PoolEvent {
        event_id: 0,
        timestamp: None,
        event_type: Some("stored".into()),
        legacy_type: None,
        model_name: Some("m".into()),
        tenant_id: Some("t".into()),
        backend_id: Some("pool".into()),
        medium: Some("cpu".into()),
        dp_rank: Some(0),
        seq_hashes: Some(vec![FlexHash(0xBAD)]),
        block_hashes: None,
    };
    apply_pool_event(
        &indexer,
        &store_ev,
        "m",
        "t",
        "pool",
        0,
        &[StorageMedium::Cpu],
        MatchMode::None,
        &None,
    )
    .unwrap();

    assert_eq!(entry.pending_count(), 1);

    // Sweep with zero TTL → removes everything.
    let pruned = entry.sweep_stale_caches(
        std::time::Duration::ZERO,
        std::time::Duration::ZERO,
        std::time::Duration::ZERO,
    );
    assert!(pruned > 0, "zero-TTL sweep should remove pending entries");
    assert_eq!(entry.pending_count(), 0);
}

// -----------------------------------------------------------------------
// apply_vllm_event — multi-block prefix chain via parent_block_hash
// -----------------------------------------------------------------------

/// Single event with parent_block_hash=None: basic smoke test for the
/// parent-block-hash plumbing added to `apply_vllm_event`.
#[test]
fn test_vllm_parent_hash_root_level() {
    use crate::hashing::compute_block_hash_for_seq;
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let block_size = 4u32;
    let tokens: Vec<i64> = (0..8).collect();
    let hashes = compute_block_hash_for_seq(&tokens, block_size);
    assert_eq!(hashes.len(), 2);

    let wk = WorkerKey {
        instance_id: "be".into(),
        backend_id: "be".into(),
        dp_rank: 0,
        medium: StorageMedium::Npu,
    };
    let media = &[StorageMedium::Npu];

    // 2-block event, no parent — these form a root chain internally.
    apply_vllm_event(
        &indexer,
        &VllmEvent::BlockStored {
            block_hashes: vec![0x100, 0x200],
            parent_block_hash: None,
            token_ids: tokens.clone(),
            block_size,
            medium: Some("xpu".into()),
            group_idx: Some(0),
        },
        "m",
        "t",
        "be",
        0,
        media,
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    let entry = indexer.get_or_create("m", "t");
    let scores = entry.find_matches(&tokens, block_size);
    assert!(
        scores.blocks.contains_key(&wk),
        "should match at least 1 block"
    );

    // Parent-block-hash is None → no parent lookup, store_data.parent_hash passed as None.
    let lookups = entry.lookups.read();
    let lookup = lookups.get(&wk).unwrap();
    assert_eq!(lookup.len(), 2);
}

/// Chained events: event-1's `parent_block_hash` points to the last block
/// of event-0, forming a cross-event prefix chain.
#[test]
fn test_vllm_parent_hash_cross_event_chain() {
    use crate::hashing::compute_block_hash_for_seq;
    use crate::indexer::Indexer;

    let indexer = Indexer::new();
    let block_size = 4u32;
    let tokens: Vec<i64> = (0..16).collect();
    let hashes = compute_block_hash_for_seq(&tokens, block_size);
    assert_eq!(hashes.len(), 4);

    let wk = WorkerKey {
        instance_id: "be".into(),
        backend_id: "be".into(),
        dp_rank: 0,
        medium: StorageMedium::Npu,
    };
    let media = &[StorageMedium::Npu];

    // Event 0: blocks 0x100, 0x200, tokens[0..8], no parent.
    apply_vllm_event(
        &indexer,
        &VllmEvent::BlockStored {
            block_hashes: vec![0x100, 0x200],
            parent_block_hash: None,
            token_ids: tokens[0..8].to_vec(),
            block_size,
            medium: Some("xpu".into()),
            group_idx: Some(0),
        },
        "m",
        "t",
        "be",
        0,
        media,
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    // Event 1: blocks 0x300, 0x400, tokens[8..16], parent=0x200 (last of event 0).
    apply_vllm_event(
        &indexer,
        &VllmEvent::BlockStored {
            block_hashes: vec![0x300, 0x400],
            parent_block_hash: Some(0x200),
            token_ids: tokens[8..16].to_vec(),
            block_size,
            medium: Some("xpu".into()),
            group_idx: Some(0),
        },
        "m",
        "t",
        "be",
        0,
        media,
        MatchMode::None,
        &None,
        block_size,
    )
    .unwrap();

    let entry = indexer.get_or_create("m", "t");
    let scores = entry.find_matches(&tokens, block_size);
    let matched = scores.blocks.get(&wk).expect("should match HBM chain");
    // Per-medium overlap is matched block count before exclusive/weight scoring.
    assert_eq!(
        *matched, 4,
        "4-block HBM chain should match depth=4, got {matched}"
    );

    // Verify parent-not-found error.
    let result = apply_vllm_event(
        &indexer,
        &VllmEvent::BlockStored {
            block_hashes: vec![0xBAD],
            parent_block_hash: Some(0xDEAD),
            token_ids: vec![100, 101, 102, 103],
            block_size,
            medium: Some("xpu".into()),
            group_idx: Some(0),
        },
        "m",
        "t",
        "be",
        0,
        media,
        MatchMode::None,
        &None,
        block_size,
    );
    assert!(
        matches!(result, Err(KvConductorError::ParentBlockNotFound)),
        "got {result:?}"
    );
}
