# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Security utility functions module"""

import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request, status

from motor.common.logger import get_logger

logger = get_logger(__name__)

MAX_PATH_LENGTH = 2048


def filter_sensitive_headers(headers) -> dict:
    """Filter sensitive information from request headers"""
    sensitive_header_keys = {
        'authorization',
        'cookie',
        'x-api-key',
        'x-auth-token',
        'proxy-authorization',
        'x-forwarded-authorization',
    }
    filtered = {}
    if hasattr(headers, 'items'):
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower in sensitive_header_keys:
                continue
            filtered[key] = value
    return filtered


def filter_sensitive_body(body: Any, max_depth: int = 3) -> Any:
    """Filter sensitive information from request body"""
    if max_depth <= 0:
        return "***MAX_DEPTH_REACHED***"

    sensitive_keys = {
        'password',
        'passwd',
        'pwd',
        'secret',
        'token',
        'api_key',
        'apikey',
        'access_token',
        'refresh_token',
        'authorization',
        'credit_card',
        'ssn',
        'social_security_number',
    }

    if isinstance(body, dict):
        filtered = {}
        for key, value in body.items():
            key_lower = str(key).lower()
            if any(sensitive in key_lower for sensitive in sensitive_keys):
                continue
            filtered[key] = filter_sensitive_body(value, max_depth - 1)
        return filtered
    elif isinstance(body, list):
        return [filter_sensitive_body(item, max_depth - 1) for item in body]
    else:
        return body


def build_safe_body_structure(body: Any, max_depth: int = 5, max_items: int = 3) -> Any:
    """Return a content-free structure summary for request tracing."""
    if max_depth <= 0:
        return {"type": type(body).__name__, "truncated": True}

    if isinstance(body, dict):
        items = list(body.items())
        return {
            "type": "dict",
            "size": len(body),
            "fields": {
                str(key): build_safe_body_structure(value, max_depth - 1, max_items) for key, value in items[:max_items]
            },
            "truncated": len(body) > max_items,
        }

    if isinstance(body, list):
        return {
            "type": "list",
            "size": len(body),
            "items": [build_safe_body_structure(item, max_depth - 1, max_items) for item in body[:max_items]],
            "truncated": len(body) > max_items,
        }

    if isinstance(body, tuple):
        return {
            "type": "tuple",
            "size": len(body),
            "items": [build_safe_body_structure(item, max_depth - 1, max_items) for item in body[:max_items]],
            "truncated": len(body) > max_items,
        }

    if isinstance(body, str):
        return {"type": "str", "length": len(body)}

    if isinstance(body, bytes):
        return {"type": "bytes", "length": len(body)}

    if body is None:
        return {"type": "null"}

    if isinstance(body, bool):
        return {"type": "bool"}

    if isinstance(body, int):
        return {"type": "int"}

    if isinstance(body, float):
        return {"type": "float"}

    return {"type": type(body).__name__}


def sanitize_error_message(error_msg: str) -> str:
    """Sanitize sensitive information from error messages (file paths, function names, internal implementation details)"""
    # Remove file paths
    error_msg = re.sub(r'[A-Za-z]:\\[^\s]+|/[^\s]+', '[FILE_PATH]', error_msg)

    # Remove file locations
    error_msg = re.sub(r'File "[^"]+", line \d+', '[FILE_LOCATION]', error_msg)
    error_msg = re.sub(r'in <[^>]+>', '', error_msg)

    # Remove stack traces
    error_msg = re.sub(r'Traceback \(most recent call last\):.*', '', error_msg, flags=re.DOTALL)

    # Generic error message, don't expose internal details
    if not error_msg or len(error_msg.strip()) == 0:
        return "An internal error occurred"

    # Limit error message length
    if len(error_msg) > 200:
        error_msg = error_msg[:200] + "..."

    return error_msg


def log_audit_event(request: Request, event_type: str, resource_name: str, event_result: str, user_id: str = None):
    client_host = request.client.host if request.client else "unknown"
    client_port = request.client.port if request.client else "unknown"

    # Get user ID (extracted from request headers or authentication information)
    if not user_id:
        auth_header = request.headers.get("Authorization", "")
        if auth_header:
            user_id = "authenticated_user"
        else:
            user_id = "anonymous"

    timestamp = datetime.now(timezone.utc).isoformat()
    request_method = request.method
    request_path = request.url.path

    logger.info(
        "AUDIT: timestamp=%s, user_id=%s, client=%s:%s, event_type=%s, resource=%s, result=%s, method=%s, path=%s",
        timestamp,
        user_id,
        client_host,
        client_port,
        event_type,
        resource_name,
        event_result,
        request_method,
        request_path,
    )


def validate_and_sanitize_path(path: str) -> str:
    """Validate and sanitize URL path, prevent path traversal and SSRF attacks"""
    # remove leading slash
    sanitized = path.lstrip('/')

    # check dangerous characters and path traversal patterns
    dangerous_patterns = [
        r'\.\.',  # path traversal
        r'//+',  # multiple slashes
        r'[<>:"|?*]',  # Windows illegal characters
        r'%2e%2e',  # URL encoded ..
        r'%2f',  # URL encoded /
        r'%5c',  # URL encoded \
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, sanitized, re.IGNORECASE):
            logger.warning(f"Detected dangerous path pattern in URL: {path}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path format")

    # check path length limit
    if len(sanitized) > MAX_PATH_LENGTH:
        logger.warning(f"Path length exceeds maximum: {len(sanitized)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Path too long")

    return sanitized


def validate_file_security(file_path: str) -> None:
    """Validate file security: check for symbolic links and file permissions"""
    file_stat = os.stat(file_path)

    # Check if file is a symbolic link (security risk)
    if os.path.islink(file_path):
        logger.error(f"Configuration file is a symbolic link, which is not allowed: {file_path}")
        raise ValueError("Symbolic links are not allowed for configuration files")

    # Check file permissions
    if hasattr(file_stat, 'st_mode'):
        file_mode = file_stat.st_mode
        if file_mode & 0o022:
            logger.warning(f"Configuration file has insecure permissions: {oct(file_mode)}")
