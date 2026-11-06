# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for motor.common.http.security_utils."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Request

from motor.common.http.security_utils import (
    MAX_PATH_LENGTH,
    build_safe_body_structure,
    filter_sensitive_body,
    filter_sensitive_headers,
    log_audit_event,
    sanitize_error_message,
    validate_and_sanitize_path,
    validate_file_security,
)

LOGGER_PATH = "motor.common.http.security_utils.logger"


def _make_request(
    method: str = "GET", path: str = "/api/v1/chat", headers=None, client=("127.0.0.1", 12345)
) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "client": client,
        "server": ("testserver", 80),
    }
    return Request(scope)


def _log_call_args(mock_logger):
    mock_logger.info.assert_called_once()
    return mock_logger.info.call_args[0]


def test_filter_sensitive_headers_drops_sensitive_keys_and_keeps_others():
    headers = {
        "Authorization": "Bearer token",
        "Cookie": "session=1",
        "X-Api-Key": "key",
        "X-Strict-Priority-Token": "strict",
        "User-Agent": "pytest",
        "Content-Type": "application/json",
    }
    assert filter_sensitive_headers(headers) == {"User-Agent": "pytest", "Content-Type": "application/json"}


def test_filter_sensitive_headers_matches_keys_case_insensitively():
    headers = {"AUTHORIZATION": "x", "Cookie": "y", "Proxy-Authorization": "z", "X-Request-Id": "1"}
    assert filter_sensitive_headers(headers) == {"X-Request-Id": "1"}


def test_filter_sensitive_headers_drops_strict_priority_token_value():
    headers = {"X-Strict-Priority-Token": "strict-secret-value", "User-Agent": "pytest"}
    filtered = filter_sensitive_headers(headers)
    assert filtered == {"User-Agent": "pytest"}
    assert "strict-secret-value" not in str(filtered)


def test_filter_sensitive_headers_accepts_framework_headers_object():
    request = _make_request(headers={"Authorization": "Bearer secret", "X-Request-Id": "1"})
    assert filter_sensitive_headers(request.headers) == {"x-request-id": "1"}


@pytest.mark.parametrize("headers", [None, ["Authorization"], "not-a-mapping", {}])
def test_filter_sensitive_headers_returns_empty_mapping_for_empty_or_non_mapping_input(headers):
    assert filter_sensitive_headers(headers) == {}


def test_filter_sensitive_body_drops_sensitive_keys():
    body = {"username": "alice", "password": "p", "access_token": "t", "credit_card": "4111"}
    assert filter_sensitive_body(body) == {"username": "alice"}


@pytest.mark.parametrize("key", ["PASSWORD", "my_token", "tokenizer", "api_key", "social_security_number"])
def test_filter_sensitive_body_matches_keys_by_case_insensitive_substring(key):
    assert filter_sensitive_body({key: "value", "keep": 1}) == {"keep": 1}


def test_filter_sensitive_body_recurses_into_nested_containers():
    assert filter_sensitive_body({"user": {"name": "alice", "password": "p"}}) == {"user": {"name": "alice"}}
    assert filter_sensitive_body([{"token": "t"}, {"id": 2}]) == [{}, {"id": 2}]


def test_filter_sensitive_body_stops_at_max_depth():
    assert filter_sensitive_body({"a": 1}, max_depth=0) == "***MAX_DEPTH_REACHED***"
    assert filter_sensitive_body({"a": {"b": 1}}, max_depth=1) == {"a": "***MAX_DEPTH_REACHED***"}


@pytest.mark.parametrize("value", ["text", 42, 1.5, True, None, {}, []])
def test_filter_sensitive_body_returns_scalars_and_empty_containers_unchanged(value):
    assert filter_sensitive_body(value) == value


def test_build_safe_body_structure_summarizes_dict_and_truncates_fields():
    result = build_safe_body_structure({"a": 1, "b": 2, "c": 3, "d": 4})
    assert result == {
        "type": "dict",
        "size": 4,
        "fields": {"a": {"type": "int"}, "b": {"type": "int"}, "c": {"type": "int"}},
        "truncated": True,
    }


def test_build_safe_body_structure_summarizes_sequences():
    assert build_safe_body_structure([1, 2, 3, 4]) == {
        "type": "list",
        "size": 4,
        "items": [{"type": "int"}, {"type": "int"}, {"type": "int"}],
        "truncated": True,
    }
    assert build_safe_body_structure((1, 2)) == {
        "type": "tuple",
        "size": 2,
        "items": [{"type": "int"}, {"type": "int"}],
        "truncated": False,
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ("hello", {"type": "str", "length": 5}),
        (b"hi", {"type": "bytes", "length": 2}),
        (None, {"type": "null"}),
        (True, {"type": "bool"}),
        (7, {"type": "int"}),
        (1.5, {"type": "float"}),
        (object(), {"type": "object"}),
    ],
)
def test_build_safe_body_structure_summarizes_scalars(value, expected):
    assert build_safe_body_structure(value) == expected


def test_build_safe_body_structure_stops_at_max_depth():
    assert build_safe_body_structure({"a": 1}, max_depth=0) == {"type": "dict", "truncated": True}
    assert build_safe_body_structure({"a": {"b": 1}}, max_depth=1) == {
        "type": "dict",
        "size": 1,
        "fields": {"a": {"type": "dict", "truncated": True}},
        "truncated": False,
    }


def test_build_safe_body_structure_respects_max_items():
    result = build_safe_body_structure({"a": 1, "b": 2}, max_items=1)
    assert result["size"] == 2
    assert list(result["fields"]) == ["a"]
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "message,leaked",
    [
        ("Failed to open /etc/passwd", "passwd"),
        ("Error at C:\\Users\\admin\\secret.txt", "secret"),
    ],
)
def test_sanitize_error_message_redacts_file_paths(message, leaked):
    result = sanitize_error_message(message)
    assert "[FILE_PATH]" in result
    assert leaked not in result


def test_sanitize_error_message_redacts_file_location():
    assert sanitize_error_message('File "config.json", line 7') == "[FILE_LOCATION]"
    result = sanitize_error_message('File "/app/main.py", line 3, in handler')
    assert "main.py" not in result
    assert "[FILE_PATH]" in result


@pytest.mark.parametrize("message", ["", "   ", "Traceback (most recent call last):\n  RuntimeError: boom"])
def test_sanitize_error_message_falls_back_when_nothing_is_left(message):
    assert sanitize_error_message(message) == "An internal error occurred"


def test_sanitize_error_message_removes_function_location():
    result = sanitize_error_message("boom in <module>")
    assert "<module>" not in result
    assert result.strip() == "boom"


def test_sanitize_error_message_keeps_plain_message():
    assert sanitize_error_message("invalid request payload") == "invalid request payload"


def test_sanitize_error_message_truncates_long_message():
    result = sanitize_error_message("x" * 500)
    assert result.endswith("...")
    assert len(result) < 500


def test_log_audit_event_logs_request_details():
    request = _make_request(method="POST", path="/api/v1/chat")
    with patch(LOGGER_PATH) as mock_logger:
        log_audit_event(request, "access", "chat", "success", user_id="alice")
    args = _log_call_args(mock_logger)
    assert args[0].startswith("AUDIT:")
    assert args[2:5] == ("alice", "127.0.0.1", 12345)
    assert args[5:8] == ("access", "chat", "success")
    assert args[8:10] == ("POST", "/api/v1/chat")
    assert args[1]


@pytest.mark.parametrize(
    "headers,client,expected_user,expected_client",
    [
        ({"Authorization": "Bearer t"}, ("10.0.0.1", 5555), "authenticated_user", ("10.0.0.1", 5555)),
        ({}, ("10.0.0.1", 5555), "anonymous", ("10.0.0.1", 5555)),
        ({}, None, "anonymous", ("unknown", "unknown")),
    ],
)
def test_log_audit_event_derives_identity(headers, client, expected_user, expected_client):
    request = _make_request(headers=headers, client=client)
    with patch(LOGGER_PATH) as mock_logger:
        log_audit_event(request, "access", "chat", "success")
    args = _log_call_args(mock_logger)
    assert args[2] == expected_user
    assert args[3:5] == expected_client


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/v1/chat", "api/v1/chat"),
        ("api/v1/chat", "api/v1/chat"),
        ("", ""),
    ],
)
def test_validate_and_sanitize_path_strips_leading_slashes(path, expected):
    assert validate_and_sanitize_path(path) == expected


@pytest.mark.parametrize(
    "path",
    ["..", "../etc/passwd", "a/../b", "//a", "///a", "a//b", "%2e%2e", "%2f", "%5C", "a?b", "a*b", "a:b"],
)
def test_validate_and_sanitize_path_rejects_dangerous_patterns(path):
    with pytest.raises(HTTPException) as exc_info:
        validate_and_sanitize_path(path)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid path format"


def test_validate_and_sanitize_path_rejects_too_long_path():
    with pytest.raises(HTTPException) as exc_info:
        validate_and_sanitize_path("a" * (MAX_PATH_LENGTH + 1))
    assert exc_info.value.status_code == 400
    assert "too long" in exc_info.value.detail


def test_validate_and_sanitize_path_accepts_path_at_length_limit():
    assert len(validate_and_sanitize_path("a" * MAX_PATH_LENGTH)) == MAX_PATH_LENGTH


def test_validate_file_security_accepts_regular_file(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    validate_file_security(str(config_file))


def test_validate_file_security_raises_for_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_file_security(str(tmp_path / "missing.json"))


def test_validate_file_security_rejects_symbolic_link():
    with patch("motor.common.http.security_utils.os.stat", return_value=SimpleNamespace(st_mode=0o600)):
        with patch("motor.common.http.security_utils.os.path.islink", return_value=True):
            with pytest.raises(ValueError, match="Symbolic links are not allowed"):
                validate_file_security("/etc/motor/config.json")


@pytest.mark.parametrize("st_mode,warned", [(0o666, True), (0o620, True), (0o600, False)])
def test_validate_file_security_warns_on_insecure_permissions(st_mode, warned):
    with patch("motor.common.http.security_utils.os.stat", return_value=SimpleNamespace(st_mode=st_mode)):
        with patch("motor.common.http.security_utils.os.path.islink", return_value=False):
            with patch(LOGGER_PATH) as mock_logger:
                validate_file_security("/etc/motor/config.json")
    assert mock_logger.warning.called is warned


def test_validate_file_security_ignores_stat_without_mode():
    with patch("motor.common.http.security_utils.os.stat", return_value=SimpleNamespace()):
        with patch("motor.common.http.security_utils.os.path.islink", return_value=False):
            with patch(LOGGER_PATH) as mock_logger:
                validate_file_security("/etc/motor/config.json")
    assert not mock_logger.warning.called
