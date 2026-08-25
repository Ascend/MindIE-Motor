# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import tempfile
from unittest.mock import Mock, patch

import pytest
import requests
from motor.common.http.http_client import SafeHTTPSClient
from motor.config.tls_config import TLSConfig


@pytest.fixture
def base_url():
    return "api.example.com"


@pytest.fixture
def cert_files():
    """test certificate files"""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.crt') as cert_file:
        cert_file.write("test_cert_content")
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.key') as key_file:
        key_file.write("test_key_content")
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ca') as ca_file:
        ca_file.write("test_ca_content")

    yield cert_file.name, key_file.name, ca_file.name

    # clear file
    os.unlink(cert_file.name)
    os.unlink(key_file.name)
    os.unlink(ca_file.name)


def test_init_with_valid_parameters(base_url, cert_files):
    """test init with valid parameters"""
    cert_file, key_file, ca_file = cert_files

    tls_config = TLSConfig(enable_tls=True, cert_file=cert_file, key_file=key_file, ca_file=ca_file)

    client = SafeHTTPSClient(address=base_url, tls_config=tls_config, timeout=10)

    assert client.base_url == f"https://{base_url}"
    assert client.timeout == 10
    assert 'User-Agent' in client.session.headers
    assert client.session.headers['User-Agent'] == 'Secure-HTTPS-Client/1.0'


def test_init_with_missing_cert_files(base_url):
    """test init with missing cert files"""
    tls_config = TLSConfig(enable_tls=True, cert_file="nonexistent.crt", key_file="nonexistent.key")
    # CertUtil.create_ssl_context returns None if cert files don't exist,
    # but client can still be initialized (SSL will fail at runtime)
    client = SafeHTTPSClient(address=base_url, tls_config=tls_config)
    # Client should still initialize, but SSL context creation may have failed
    assert client.base_url == f"https://{base_url}"
    assert client.protocol == 'https://'


def test_init_without_certificates(base_url):
    """test init without certs"""
    client = SafeHTTPSClient(address=base_url)

    assert client.base_url == f"http://{base_url}"
    assert client.protocol == 'http://'


def test_init_applies_custom_headers(base_url):
    client = SafeHTTPSClient(address=base_url, headers={"X-Motor-Management-Key": "test-key"})

    assert client.session.headers["X-Motor-Management-Key"] == "test-key"


def test_url_construction(base_url):
    """test url construction"""
    client = SafeHTTPSClient(address=base_url)

    client_with_slash = SafeHTTPSClient(address=base_url + "/")
    assert client_with_slash.base_url == f"http://{base_url}"

    with patch.object(client.session, 'request') as mock_request:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_request.return_value = mock_response

        client.get("/test-endpoint")
        called_url = mock_request.call_args[1]['url']
        assert called_url == f"http://{base_url}/test-endpoint"

        client.get("test-endpoint")
        called_url = mock_request.call_args[1]['url']
        assert called_url == f"http://{base_url}/test-endpoint"


@pytest.mark.parametrize("method", ['GET', 'POST'])
def test_successful_requests(base_url, method):
    """test successful requests"""
    client = SafeHTTPSClient(address=base_url)

    with patch.object(client.session, 'request') as mock_request:
        expected_response = {"status": "success", "data": "test"}
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = expected_response
        mock_request.return_value = mock_response

        if method == 'GET':
            response = client.get("/test", params={"key": "value"})
        else:
            response = client.post("/test", data={"key": "value"})

        assert response == expected_response

        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs['method'] == method
        assert call_kwargs['url'] == f"http://{base_url}/test"


def test_ssl_error_handling(base_url):
    """test ssl error handling"""
    client = SafeHTTPSClient(address=base_url)

    with patch.object(client.session, 'request') as mock_request:
        mock_request.side_effect = requests.exceptions.SSLError("SSL certificate verification failed")

        with pytest.raises(Exception, match="SSL verify failed:"):
            client.get("/test")


def test_http_error_handling(base_url):
    """test http error handling"""
    client = SafeHTTPSClient(address=base_url)

    with patch.object(client.session, 'request') as mock_request:
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_request.side_effect = requests.exceptions.HTTPError(response=mock_response)

        with pytest.raises(Exception, match="http response error 404"):
            client.get("/test")


def test_generic_exception_handling(base_url):
    """test generic exception handling"""
    client = SafeHTTPSClient(address=base_url)

    with patch.object(client.session, 'request') as mock_request:
        mock_request.side_effect = Exception("Generic error")

        with pytest.raises(Exception, match="send request .* error:"):
            client.get("/test")


def test_context_manager(base_url):
    """test context manager"""
    with patch.object(requests.Session, 'close') as mock_close:
        with SafeHTTPSClient(address=base_url) as client:
            assert isinstance(client, SafeHTTPSClient)

        mock_close.assert_called_once()


def test_close_method(base_url):
    """test close method"""
    client = SafeHTTPSClient(address=base_url)

    with patch.object(client.session, 'close') as mock_close:
        client.close()
        mock_close.assert_called_once()


def test_request_timeout(base_url):
    """test request timeout"""
    client = SafeHTTPSClient(address=base_url, timeout=3.5)

    with patch.object(client.session, 'request') as mock_request:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_request.return_value = mock_response

        client.get("/test")

        call_kwargs = mock_request.call_args[1]
        assert call_kwargs['timeout'] == 3.5


# ── post_bytes / msgpack error extraction ───────────────────────────────


def test_post_bytes_sends_raw_body(base_url):
    """post_bytes sends the raw body with msgpack Content-Type headers."""
    client = SafeHTTPSClient(address=base_url)
    body = b"\x81\xa5hello\xa5world"

    with patch.object(client.session, 'request') as mock_request:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/msgpack"}
        mock_request.return_value = mock_response

        resp = client.post_bytes("/query", body)

    assert resp is mock_response
    kwargs = mock_request.call_args[1]
    assert kwargs["data"] == body
    assert kwargs["headers"]["Content-Type"] == "application/msgpack"
    assert kwargs["headers"]["Accept"] == "application/msgpack"


def test_post_bytes_json_headers_override(base_url):
    """post_bytes honors explicit content_type/accept overrides."""
    client = SafeHTTPSClient(address=base_url)

    with patch.object(client.session, 'request') as mock_request:
        mock_request.return_value = Mock(status_code=200, headers={})
        client.post_bytes("/query", b"x", content_type="application/json", accept="application/json")

    headers = mock_request.call_args[1]["headers"]
    assert headers["Content-Type"] == "application/json"


def _error_response(content_type, content, text=""):
    mock_response = Mock()
    mock_response.status_code = 404
    mock_response.headers = {"Content-Type": content_type}
    mock_response.content = content
    mock_response.text = text
    return mock_response


def test_http_error_msgpack_body_extracts_error(base_url):
    """msgpack error bodies surface the readable error field."""
    import msgspec

    client = SafeHTTPSClient(address=base_url)
    err_body = msgspec.msgpack.encode({"error": "no indexer for model=x"})
    mock_response = _error_response("application/msgpack", err_body)

    with patch.object(client.session, 'request') as mock_request:
        mock_request.side_effect = requests.exceptions.HTTPError(response=mock_response)
        with pytest.raises(Exception, match="no indexer for model=x"):
            client.get("/query")


def test_http_error_json_body_extracts_error(base_url):
    """JSON error bodies surface the readable error field."""
    client = SafeHTTPSClient(address=base_url)
    mock_response = _error_response("application/json", b'{"error": "boom-json"}', text="ignored")
    mock_response.json.return_value = {"error": "boom-json"}

    with patch.object(client.session, 'request') as mock_request:
        mock_request.side_effect = requests.exceptions.HTTPError(response=mock_response)
        with pytest.raises(Exception, match="boom-json"):
            client.get("/query")


def test_http_error_non_error_body_falls_back_to_text(base_url):
    """Non-map / undecodable error bodies fall back to the raw text."""
    client = SafeHTTPSClient(address=base_url)
    mock_response = _error_response("text/plain", b"gateway down", text="gateway down")

    with patch.object(client.session, 'request') as mock_request:
        mock_request.side_effect = requests.exceptions.HTTPError(response=mock_response)
        with pytest.raises(Exception, match="gateway down"):
            client.get("/query")
