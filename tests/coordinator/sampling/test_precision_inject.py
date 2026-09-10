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

import logging

import pytest

from motor.config.coordinator import PrecisionDetectionConfig
from motor.coordinator.router.precision_sample.request import (
    LogprobsRequestMetadata,
    inject_logprobs,
    logger as request_logger,
)
from motor.coordinator.router.precision_sample.response import project_logprobs_for_client


def _cfg(logprobs_count: int = 5) -> PrecisionDetectionConfig:
    return PrecisionDetectionConfig(logprobs_count=logprobs_count)


class TestInjectCompletion:
    def test_no_field_is_injected(self) -> None:
        req = {"prompt": "Hello", "request_id": "r1"}
        inject_logprobs(req, _cfg(3), req_id="r1")
        assert req["logprobs"] == 3
        assert req["return_token_ids"] is True
        assert "top_logprobs" not in req

    @pytest.mark.parametrize("bad", [None, 0, False])
    def test_invalid_client_value_is_overridden(self, bad, caplog) -> None:
        req = {"prompt": "Hello", "logprobs": bad, "request_id": "r1"}
        with caplog.at_level(logging.INFO, logger=request_logger.name):
            inject_logprobs(req, _cfg(3), req_id="r1")
        assert req["logprobs"] == 3
        assert req["return_token_ids"] is True
        assert any("overridden" in r.message for r in caplog.records), caplog.records

    def test_consistent_client_value_no_override_log(self, caplog) -> None:
        req = {"prompt": "Hello", "logprobs": 3, "request_id": "r1"}
        with caplog.at_level(logging.INFO, logger=request_logger.name):
            inject_logprobs(req, _cfg(3), req_id="r1")
        assert req["logprobs"] == 3
        # Same value → no override INFO; the function still emits DEBUG.
        assert not any("overridden" in r.message for r in caplog.records)

    def test_client_count_is_not_reduced(self) -> None:
        req = {"prompt": "Hello", "logprobs": 5}

        metadata = inject_logprobs(req, _cfg(1), req_id="r1")

        assert req["logprobs"] == 5
        assert metadata.client_count == 5
        assert metadata.effective_count == 5

    def test_boolean_logprobs_true_counts_as_client_requested(self) -> None:
        """Completion logprobs=true is a valid switch even though top-k width is unspecified."""
        req = {"prompt": "Hello", "logprobs": True, "request_id": "r1"}

        metadata = inject_logprobs(req, _cfg(3), req_id="r1")

        assert req["logprobs"] == 3
        assert metadata.client_requested is True
        assert metadata.client_count == 0
        assert metadata.effective_count == 3

    def test_boolean_logprobs_true_exit_projection_keeps_logprobs(self) -> None:
        """Regression: completion logprobs=true must not strip choices' logprobs on exit."""
        req = {"prompt": "Hello", "logprobs": True, "request_id": "r1"}
        metadata = inject_logprobs(req, _cfg(3), req_id="r1")
        body = {
            "choices": [
                {
                    "logprobs": {
                        "top_logprobs": [
                            {"token_id:1": -0.1, "token_id:2": -0.2, "token_id:3": -0.3},
                        ]
                    }
                }
            ]
        }

        project_logprobs_for_client(body, metadata=metadata)
        assert "logprobs" in body["choices"][0]
        assert len(body["choices"][0]["logprobs"]["top_logprobs"][0]) == 3

    def test_request_id_in_log(self, caplog) -> None:
        req = {"prompt": "Hello", "logprobs": None, "request_id": "r42"}
        with caplog.at_level(logging.INFO, logger=request_logger.name):
            inject_logprobs(req, _cfg(2), req_id="r42")
        assert any("req_id=r42" in r.message for r in caplog.records)

    def test_missing_req_id_does_not_crash(self) -> None:
        req = {"prompt": "Hello"}
        inject_logprobs(req, _cfg(2), req_id="")
        assert req["logprobs"] == 2
        assert req["return_token_ids"] is True


class TestInjectChat:
    def test_no_field_is_injected(self) -> None:
        req = {
            "messages": [{"role": "user", "content": "Hi"}],
            "request_id": "r1",
        }
        inject_logprobs(req, _cfg(4), req_id="r1")
        assert req["logprobs"] is True
        assert req["top_logprobs"] == 4
        assert req["return_token_ids"] is True

    @pytest.mark.parametrize(
        "bad_lp,bad_top",
        [(False, None), (None, False), (0, 0), (None, 99)],
    )
    def test_invalid_client_value_is_overridden(self, bad_lp, bad_top, caplog) -> None:
        req = {
            "messages": [{"role": "user", "content": "Hi"}],
            "logprobs": bad_lp,
            "top_logprobs": bad_top,
            "request_id": "r1",
        }
        with caplog.at_level(logging.INFO, logger=request_logger.name):
            inject_logprobs(req, _cfg(3), req_id="r1")
        assert req["logprobs"] is True
        assert req["top_logprobs"] == 3
        assert req["return_token_ids"] is True
        assert any("overridden" in r.message for r in caplog.records)

    def test_consistent_chat_values_no_override(self, caplog) -> None:
        req = {
            "messages": [{"role": "user", "content": "Hi"}],
            "logprobs": True,
            "top_logprobs": 4,
        }
        with caplog.at_level(logging.INFO, logger=request_logger.name):
            inject_logprobs(req, _cfg(4), req_id="r1")
        assert req["logprobs"] is True
        assert req["top_logprobs"] == 4
        assert not any("overridden" in r.message for r in caplog.records)

    def test_client_top_logprobs_is_not_reduced(self) -> None:
        req = {
            "messages": [{"role": "user", "content": "Hi"}],
            "logprobs": True,
            "top_logprobs": 5,
        }

        metadata = inject_logprobs(req, _cfg(1), req_id="r1")

        assert req["top_logprobs"] == 5
        assert metadata.client_count == 5
        assert metadata.effective_count == 5

    @pytest.mark.parametrize("truthy_lp", [1, "yes"])
    def test_truthy_non_bool_logprobs_counts_as_client_requested(self, truthy_lp) -> None:
        """Chat clients may send 1 or other truthy values instead of strict True;
        the exit projection must not strip logprobs they explicitly asked for.
        """
        req = {
            "messages": [{"role": "user", "content": "Hi"}],
            "logprobs": truthy_lp,
            "top_logprobs": 2,
        }

        metadata = inject_logprobs(req, _cfg(5), req_id="r1")

        assert req["logprobs"] is True
        assert req["top_logprobs"] == 5
        assert metadata.client_requested is True
        assert metadata.client_count == 2

    def test_logprobs_true_without_top_logprobs_skips_width_trim(self) -> None:
        """Chat logprobs=true without top_logprobs must not trim candidates to an empty list."""
        req = {
            "messages": [{"role": "user", "content": "Hi"}],
            "logprobs": True,
        }
        metadata = inject_logprobs(req, _cfg(5), req_id="r1")
        candidates = [{"token": str(index), "logprob": -index} for index in range(5)]
        body = {"choices": [{"logprobs": {"content": [{"token": "x", "top_logprobs": candidates}]}}]}

        assert not project_logprobs_for_client(body, metadata=metadata)
        assert len(body["choices"][0]["logprobs"]["content"][0]["top_logprobs"]) == 5

    def test_truthy_chat_logprobs_exit_projection_keeps_logprobs(self) -> None:
        """Regression: logprobs=1 must not have choices' logprobs popped on exit."""
        req = {
            "messages": [{"role": "user", "content": "Hi"}],
            "logprobs": 1,
            "top_logprobs": 2,
        }
        metadata = inject_logprobs(req, _cfg(5), req_id="r1")
        body = {
            "choices": [
                {
                    "logprobs": {
                        "content": [
                            {
                                "token": "x",
                                "top_logprobs": [{"token": str(i), "logprob": -i} for i in range(5)],
                            }
                        ]
                    }
                }
            ]
        }

        assert project_logprobs_for_client(body, metadata=metadata)
        content = body["choices"][0]["logprobs"]["content"]
        assert len(content[0]["top_logprobs"]) == 2


class TestInjectReturnTokenIds:
    def test_return_token_ids_always_set_true(self) -> None:
        req = {"prompt": "Hi", "return_token_ids": False, "logprobs": None}
        inject_logprobs(req, _cfg(2), req_id="r1")
        assert req["return_token_ids"] is True


class TestProjectLogprobsForClient:
    def test_removes_logprobs_when_only_motor_requested_them(self) -> None:
        body = {"choices": [{"logprobs": {"content": []}}]}
        metadata = LogprobsRequestMetadata(
            is_chat=True,
            client_requested=False,
            client_count=0,
            effective_count=5,
        )

        assert project_logprobs_for_client(body, metadata=metadata)
        assert "logprobs" not in body["choices"][0]

    def test_trims_chat_candidates_to_client_count(self) -> None:
        candidates = [{"token": str(index), "logprob": -index} for index in range(5)]
        body = {"choices": [{"logprobs": {"content": [{"token": "x", "top_logprobs": candidates}]}}]}
        metadata = LogprobsRequestMetadata(
            is_chat=True,
            client_requested=True,
            client_count=2,
            effective_count=5,
        )

        assert project_logprobs_for_client(body, metadata=metadata)
        assert len(body["choices"][0]["logprobs"]["content"][0]["top_logprobs"]) == 2

    def test_trims_completion_candidates_to_client_count(self) -> None:
        body = {
            "choices": [
                {
                    "logprobs": {
                        "top_logprobs": [
                            {"token_id:1": -0.1, "token_id:2": -0.2, "token_id:3": -0.3},
                        ]
                    }
                }
            ]
        }
        metadata = LogprobsRequestMetadata(
            is_chat=False,
            client_requested=True,
            client_count=2,
            effective_count=3,
        )

        assert project_logprobs_for_client(body, metadata=metadata)
        assert list(body["choices"][0]["logprobs"]["top_logprobs"][0]) == ["token_id:1", "token_id:2"]
