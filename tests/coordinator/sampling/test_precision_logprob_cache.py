# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

from motor.coordinator.router.precision_sample.response import (
    _parse_logprob_token_id,
    update_logprob_cache,
)


def test_parse_logprob_token_id_valid() -> None:
    assert _parse_logprob_token_id("token_id:123") == 123
    assert _parse_logprob_token_id("token_id:0") == 0
    assert _parse_logprob_token_id("token_id:9999999") == 9999999


def test_parse_logprob_token_id_invalid() -> None:
    assert _parse_logprob_token_id("hello") is None
    assert _parse_logprob_token_id("token_id:") is None
    assert _parse_logprob_token_id("token_id:abc") is None
    assert _parse_logprob_token_id(123) is None
    assert _parse_logprob_token_id(None) is None


def test_chat_topk1_uses_token_id_fallback() -> None:
    """logprobs_count=1: build single-key dict from content[].logprob + token_ids tail."""
    request_info: dict = {
        "cached_output_token_ids": [101, 102, 103],
    }
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [
                        {"token": "a", "logprob": -0.1},
                        {"token": "b", "logprob": -0.2},
                        {"token": "c", "logprob": -0.3},
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=1)
    assert request_info["cached_logprobs"] == [-0.1, -0.2, -0.3]
    assert request_info["cached_topk_logprobs"] == [
        {101: -0.1},
        {102: -0.2},
        {103: -0.3},
    ]


def test_chat_topk5_parses_top_logprobs() -> None:
    """logprobs_count=5: parse content[].top_logprobs with token_id: labels."""
    request_info: dict = {"cached_output_token_ids": [201]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [
                        {
                            "token": "token_id:201",
                            "logprob": -0.5,
                            "top_logprobs": [
                                {"token": "token_id:201", "logprob": -0.5},
                                {"token": "token_id:202", "logprob": -1.2},
                                {"token": "token_id:203", "logprob": -2.0},
                            ],
                        },
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    assert request_info["cached_logprobs"] == [-0.5]
    assert request_info["cached_topk_logprobs"] == [
        {201: -0.5, 202: -1.2, 203: -2.0},
    ]


def test_chat_topk1_no_token_ids_yields_no_topk() -> None:
    request_info: dict = {}  # no cached_output_token_ids
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [{"token": "x", "logprob": -0.1}],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=1)
    assert request_info["cached_logprobs"] == [-0.1]
    # topk=1 needs the actual sampled token id; without it we skip (not a fatal error,
    # the checker will fail-open on the resulting length mismatch).
    assert "cached_topk_logprobs" not in request_info


def test_completion_path_only_floats() -> None:
    """Completion responses don't get topk parsed (no plan support)."""
    request_info: dict = {}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.1, -0.2, None, -0.4],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    assert request_info["cached_logprobs"] == [-0.1, -0.2, -0.4]
    assert "cached_topk_logprobs" not in request_info


def test_chat_content_aligned_with_token_ids_tail() -> None:
    """Each chunk's content[] tail is aligned with the token_ids tail consumed."""
    request_info: dict = {"cached_output_token_ids": [1, 2, 3, 4, 5]}
    # Simulate a chunk that has the last 2 tokens. Pre-populated logprobs are
    # extend-only, so we just verify this chunk consumes 2 ids from the tail.
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [
                        {"token": "x", "logprob": -0.4},
                        {"token": "y", "logprob": -0.5},
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=1)
    assert request_info["cached_topk_logprobs"] == [{4: -0.4}, {5: -0.5}]


def test_chat_topk1_token_ids_mismatch() -> None:
    """When content[] exceeds available token_ids, only matched entries get topk."""
    request_info: dict = {"cached_output_token_ids": [1]}  # only 1 id
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [
                        {"token": "a", "logprob": -0.1},
                        {"token": "b", "logprob": -0.2},
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=1)
    # Only the first entry has a fallback tid, so the second is dropped.
    assert request_info["cached_topk_logprobs"] == [{1: -0.1}]


def test_chat_topk5_falls_back_to_sampled_when_top_logprobs_missing() -> None:
    """logprobs_count>1: when top_logprobs is absent, sampled token must still be there."""
    request_info: dict = {"cached_output_token_ids": [777]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [
                        # No top_logprobs at all (engine degraded to top-1 only).
                        {"token": "token_id:777", "logprob": -0.4},
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    assert request_info["cached_topk_logprobs"] == [{777: -0.4}]


def test_chat_topk5_falls_back_when_top_logprobs_unparseable() -> None:
    """logprobs_count>1: when top_logprobs tokens are decoded strings, sampled still present."""
    request_info: dict = {"cached_output_token_ids": [42]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [
                        {
                            "token": "token_id:42",
                            "logprob": -0.9,
                            "top_logprobs": [
                                # engine didn't honour return_tokens_as_token_ids
                                {"token": "你", "logprob": -0.9},
                                {"token": "好", "logprob": -1.5},
                            ],
                        },
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    # Sampled token (42) must be present; decoded-string candidates dropped.
    assert request_info["cached_topk_logprobs"] == [{42: -0.9}]


def test_chat_topk5_sampled_overrides_top_logprobs_mismatch() -> None:
    """Sampled-token value (entry.logprob) wins over an inconsistent top candidate."""
    request_info: dict = {"cached_output_token_ids": [10]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "content": [
                        {
                            "token": "token_id:10",
                            "logprob": -0.2,  # authoritative value
                            "top_logprobs": [
                                # engine reports a different logprob for the
                                # sampled token — sampled token always wins.
                                {"token": "token_id:10", "logprob": -9.9},
                            ],
                        },
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    assert request_info["cached_topk_logprobs"] == [{10: -0.2}]


def test_no_choices_or_no_logprobs_noop() -> None:
    request_info: dict = {}
    update_logprob_cache(request_info, {}, logprobs_count=1)
    update_logprob_cache(
        request_info,
        {"choices": [{"logprobs": None}]},
        logprobs_count=1,
    )
    assert "cached_logprobs" not in request_info
    assert "cached_topk_logprobs" not in request_info


def test_chunk_with_token_ids_but_null_logprobs_warns_once(caplog) -> None:
    """When token_ids arrive but logprobs is null, emit a single WARN per request."""
    import logging

    from motor.coordinator.router.precision_sample import response as resp_mod

    request_info: dict = {}
    chunk = {
        "choices": [
            {
                "token_ids": [1001, 1002, 1003],
                "logprobs": None,
            },
        ],
    }
    with caplog.at_level(logging.WARNING, logger=resp_mod.logger.name):
        update_logprob_cache(request_info, chunk, logprobs_count=1)
        # Simulate a second chunk for the same request; should NOT warn again.
        update_logprob_cache(request_info, chunk, logprobs_count=1)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.message for r in warns]
    assert "missing logprobs" in warns[0].message
    assert "token_ids_in_chunk=3" in warns[0].message


def test_completion_logprobs_all_null_warns_once(caplog) -> None:
    """When Completion logprobs object is present but token_logprobs are all null, warn once."""
    import logging

    from motor.coordinator.router.precision_sample import response as resp_mod

    request_info: dict = {}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [None, None, None],
                },
            },
        ],
    }
    with caplog.at_level(logging.WARNING, logger=resp_mod.logger.name):
        update_logprob_cache(request_info, chunk, logprobs_count=1)
        update_logprob_cache(request_info, chunk, logprobs_count=1)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.message for r in warns]
    assert "completion logprobs all null" in warns[0].message
    assert "raw_count=3" in warns[0].message


def test_completion_token_logprobs_partial_keeps_values_silently() -> None:
    """If only some token_logprobs are non-null, keep the non-null ones (no WARN)."""
    import logging

    from motor.coordinator.router.precision_sample import response as resp_mod

    request_info: dict = {}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.1, None, -0.3],
                },
            },
        ],
    }
    import io

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    resp_mod.logger.addHandler(handler)
    try:
        update_logprob_cache(request_info, chunk, logprobs_count=1)
    finally:
        resp_mod.logger.removeHandler(handler)
    assert request_info["cached_logprobs"] == [-0.1, -0.3]
    assert "all null" not in log_stream.getvalue()


# --- Completion topk parity (per plan "enhance-completion-cache") ---


def test_completion_topk1_uses_token_id_fallback() -> None:
    """logprobs_count=1: build single-key dict from token_logprobs + cached_output_token_ids tail."""
    request_info: dict = {"cached_output_token_ids": [201, 202, 203]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.1, -0.2, -0.3],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=1)
    assert request_info["cached_logprobs"] == [-0.1, -0.2, -0.3]
    assert request_info["cached_topk_logprobs"] == [
        {201: -0.1},
        {202: -0.2},
        {203: -0.3},
    ]


def test_completion_topk5_parses_token_id_labels() -> None:
    """logprobs_count>1: parse top_logprobs[i] entries with token_id: labels into multi-key dicts."""
    request_info: dict = {"cached_output_token_ids": [301]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.5],
                    "top_logprobs": [
                        {
                            "token_id:301": -0.5,
                            "token_id:302": -1.2,
                            "token_id:303": -2.0,
                        }
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    assert request_info["cached_logprobs"] == [-0.5]
    assert request_info["cached_topk_logprobs"] == [
        {301: -0.5, 302: -1.2, 303: -2.0},
    ]


def test_completion_topk1_no_token_ids_yields_no_topk() -> None:
    """Without cached_output_token_ids tail, topk cannot anchor and is skipped."""
    request_info: dict = {}  # no cached_output_token_ids
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.1, -0.2],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=1)
    assert request_info["cached_logprobs"] == [-0.1, -0.2]
    # No token_ids tail → sampled-token fallback has nothing to anchor → no topk.
    assert "cached_topk_logprobs" not in request_info


def test_completion_sampled_token_overrides_top_mismatch() -> None:
    """token_logprobs value wins over an inconsistent top_logprobs candidate for the sampled token."""
    request_info: dict = {"cached_output_token_ids": [42]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.2],  # authoritative
                    "top_logprobs": [
                        {
                            "token_id:42": -9.9,  # inconsistent
                            "token_id:99": -1.5,
                        }
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    assert request_info["cached_topk_logprobs"] == [{42: -0.2, 99: -1.5}]


def test_completion_token_logprobs_partial_skips_null() -> None:
    """Null entries in token_logprobs are skipped (both for floats and topk)."""
    request_info: dict = {"cached_output_token_ids": [11, 12, 13]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.1, None, -0.3],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=1)
    assert request_info["cached_logprobs"] == [-0.1, -0.3]
    assert request_info["cached_topk_logprobs"] == [{11: -0.1}, {13: -0.3}]


def test_completion_top_logprobs_unparseable_keys_dropped() -> None:
    """top_logprobs entries with non-numeric keys are dropped (sampled-token fallback still applies)."""
    request_info: dict = {"cached_output_token_ids": [7]}
    chunk = {
        "choices": [
            {
                "logprobs": {
                    "token_logprobs": [-0.4],
                    "top_logprobs": [
                        {
                            "你": -0.4,
                            "好": -1.5,
                        }
                    ],
                },
            },
        ],
    }
    update_logprob_cache(request_info, chunk, logprobs_count=5)
    # Sampled token (7) must be present; decoded-string candidates dropped.
    assert request_info["cached_topk_logprobs"] == [{7: -0.4}]
