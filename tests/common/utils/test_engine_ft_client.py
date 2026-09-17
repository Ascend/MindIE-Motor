# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the vLLM fault-tolerance HTTP contract."""

from unittest.mock import patch

import pytest

from motor.common.http.engine_ft_client import (
    EngineFtApplyError,
    apply_engine_ft_instruction,
    apply_engine_ft_instructions,
    query_engine_ft_entries,
    query_engine_ft_entry,
    query_engine_ft_status,
)
from motor.common.resources.endpoint import Endpoint


def _endpoint() -> Endpoint:
    return Endpoint(id=2, ip="192.0.2.10", business_port="8000")


@patch("motor.common.http.engine_ft_client.SafeHTTPSClient")
def test_query_uses_versioned_status_path(mock_client_cls):
    client = mock_client_cls.return_value
    client.__enter__.return_value = client
    client.get.return_value = {"engines": []}

    assert query_engine_ft_status(_endpoint(), timeout=3) == {"engines": []}

    client.get.assert_called_once_with("/v1/fault_tolerance/status")


@patch("motor.common.http.engine_ft_client.SafeHTTPSClient")
def test_apply_uses_vllm_async_contract(mock_client_cls):
    client = mock_client_cls.return_value
    client.__enter__.return_value = client
    response = client.do_post.return_value
    response.status_code = 202
    response.json.return_value = {"message": "accepted", "request_id": "ft-1"}

    result = apply_engine_ft_instruction(
        _endpoint(),
        "scale_down",
        {"removed_dp_ranks": [1]},
        request_id="ft-1",
        timeout=4,
    )

    assert result["request_id"] == "ft-1"
    client.do_post.assert_called_once_with(
        "/v1/fault_tolerance/apply",
        data={
            "instruction": "scale_down",
            "params": {"removed_dp_ranks": [1]},
            "request_id": "ft-1",
        },
    )


@patch("motor.common.http.engine_ft_client.SafeHTTPSClient")
def test_status_does_not_fall_back_when_versioned_path_fails(mock_client_cls):
    client = mock_client_cls.return_value
    client.__enter__.return_value = client
    client.get.side_effect = RuntimeError("http response error 404, missing")

    with pytest.raises(RuntimeError, match="404"):
        query_engine_ft_status(_endpoint())
    client.get.assert_called_once_with("/v1/fault_tolerance/status")


@pytest.mark.parametrize("instruction", ["restart"])
def test_apply_rejects_non_scale_down_instruction(instruction):
    with pytest.raises(ValueError, match="unsupported"):
        apply_engine_ft_instruction(_endpoint(), instruction)


@patch("motor.common.http.engine_ft_client.SafeHTTPSClient")
def test_apply_accepts_retry_instruction(mock_client_cls):
    client = mock_client_cls.return_value
    client.__enter__.return_value = client
    client.do_post.return_value.status_code = 202
    client.do_post.return_value.json.return_value = {"message": "accepted"}

    apply_engine_ft_instruction(_endpoint(), "retry", request_id="retry-1")

    assert client.do_post.call_args.kwargs["data"]["instruction"] == "retry"


@patch("motor.common.http.engine_ft_client.SafeHTTPSClient")
def test_apply_rejection_exposes_http_status_without_retry_classification(
    mock_client_cls,
):
    client = mock_client_cls.return_value
    client.__enter__.return_value = client
    client.do_post.return_value.status_code = 503

    with pytest.raises(EngineFtApplyError) as error:
        apply_engine_ft_instruction(_endpoint(), "scale_down")

    assert error.value.status_code == 503


@patch("motor.common.http.engine_ft_client.query_engine_ft_status")
def test_query_entry_selects_matching_rank(mock_query):
    mock_query.return_value = {
        "engines": [
            {"id": 1, "status": "healthy"},
            {"id": 2, "status": "unhealthy"},
        ]
    }

    assert query_engine_ft_entry(_endpoint()) == {"id": 2, "status": "unhealthy"}


@patch("motor.common.http.engine_ft_client.query_engine_ft_status")
def test_query_entry_rejects_single_mismatched_global_dp_rank(mock_query):
    mock_query.return_value = {"engines": [{"id": 1, "status": "unhealthy"}]}

    with pytest.raises(ValueError, match="no matching rank"):
        query_engine_ft_entry(_endpoint())


@patch("motor.common.http.engine_ft_client.query_engine_ft_entry")
def test_query_entries_returns_status_by_endpoint_id(mock_query):
    endpoints = [
        Endpoint(id=1, ip="192.0.2.1", business_port="8000"),
        Endpoint(id=2, ip="192.0.2.2", business_port="8000"),
    ]
    mock_query.side_effect = lambda endpoint, **_kwargs: {
        "id": endpoint.id,
        "status": "healthy" if endpoint.id == 1 else "dead",
    }

    assert query_engine_ft_entries(endpoints) == {
        1: {"id": 1, "status": "healthy"},
        2: {"id": 2, "status": "dead"},
    }


@patch("motor.common.http.engine_ft_client.apply_engine_ft_instruction")
def test_apply_instructions_dispatches_once_to_every_endpoint(mock_apply):
    endpoints = [
        Endpoint(id=1, ip="192.0.2.1", business_port="8000"),
        Endpoint(id=2, ip="192.0.2.2", business_port="8000"),
    ]

    apply_engine_ft_instructions(
        endpoints,
        "scale_down",
        {"removed_dp_ranks": [3]},
        "request-1",
        3,
    )

    assert mock_apply.call_count == 2
