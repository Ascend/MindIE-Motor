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

from motor.config.coordinator import TokenSamplingConfig
from motor.coordinator.router.precision_sample.request import (
    inject_logprobs,
    logger as request_logger,
)


def _cfg(logprobs_count: int = 5) -> TokenSamplingConfig:
    return TokenSamplingConfig(logprobs_count=logprobs_count)


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


class TestInjectReturnTokenIds:
    def test_return_token_ids_always_set_true(self) -> None:
        req = {"prompt": "Hi", "return_token_ids": False, "logprobs": None}
        inject_logprobs(req, _cfg(2), req_id="r1")
        assert req["return_token_ids"] is True
