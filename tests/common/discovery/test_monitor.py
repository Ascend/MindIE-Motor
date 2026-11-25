# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading
import time
from unittest.mock import Mock, patch

import pytest
from requests import HTTPError, Response

from motor.common.discovery.monitor import (
    DiscoveryUnavailable,
    EndpointMonitor,
    ProbeResult,
    probe_endpoint,
)
from motor.common.http.http_client import HttpStatusError
from motor.config.service_discovery import ServiceDiscoveryConfig, ServiceEndpoint
from motor.config.tls_config import TLSConfig


def _config(service="controller", count=2, interval=0.02):
    ports = {"api": 2026} if service == "controller" else {"management": 1026, "observability": 1027}
    return ServiceDiscoveryConfig(
        probe_interval_seconds=interval,
        endpoints={service: [ServiceEndpoint("192.0.2." + str(index + 1), ports) for index in range(count)]},
    )


def _wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Background observation did not arrive")
        threading.Event().wait(0.005)


@pytest.mark.parametrize(
    "service,status,body,expected",
    [
        ("controller", 200, {}, ProbeResult(True, True)),
        ("controller", 503, {}, ProbeResult(False, False)),
        ("coordinator", 200, {"ready": False}, ProbeResult(True, False)),
        ("coordinator", 200, {"ready": True}, ProbeResult(True, True)),
        ("coordinator", 503, {}, ProbeResult(False, False)),
    ],
)
def test_http_probe_preserves_role_and_business_ready_distinction(service, status, body, expected):
    endpoint = _config(service).endpoints[service][0]
    response = Mock(status_code=status)
    response.json.return_value = body
    client = Mock()
    client.do_get.side_effect = HttpStatusError(status, "standby") if status == 503 else None
    client.do_get.return_value = response
    tls = TLSConfig(enable_tls=True)
    with patch("motor.common.discovery.monitor.SafeHTTPSClient") as factory:
        factory.return_value.__enter__.return_value = client
        assert probe_endpoint(service, endpoint, tls, 0.5) == expected
        assert factory.call_args.kwargs == {"tls_config": tls, "timeout": 0.5}
        client.do_get.assert_called_once_with("/readiness")


def test_probe_marks_http_and_transport_errors_unavailable():
    response = Response()
    response.status_code = 503
    client = Mock()
    client.do_get.side_effect = HTTPError(response=response)
    with patch("motor.common.discovery.monitor.SafeHTTPSClient") as factory:
        factory.return_value.__enter__.return_value = client
        endpoint = _config("coordinator").endpoints["coordinator"][0]
        assert probe_endpoint("coordinator", endpoint, None, 1) == ProbeResult(False, False)
        client.do_get.side_effect = RuntimeError("connection refused")
        assert probe_endpoint("coordinator", endpoint, None, 1) == ProbeResult(False, False)


def test_slow_member_does_not_block_other_probes_or_business_reads():
    blocked, release = threading.Event(), threading.Event()
    calls = []

    def probe(endpoint):
        calls.append(endpoint.host)
        if endpoint.host.endswith(".1"):
            blocked.set()
            release.wait(2)
        return ProbeResult(True, True)

    monitor = EndpointMonitor("controller", _config(), probe=probe)
    assert len(monitor.states()) == 2
    assert not calls
    with pytest.raises(DiscoveryUnavailable):
        monitor.select("api")
    monitor.start()
    try:
        assert blocked.wait(1)
        _wait_until(lambda: monitor.states()[1].eligible)
        before = len(calls)
        assert monitor.select("api").host == "192.0.2.2"
        assert len(calls) == before
        assert not monitor.states()[0].eligible
        _wait_until(lambda: calls.count("192.0.2.2") >= 3)
        assert calls.count("192.0.2.1") == 1
    finally:
        release.set()
        monitor.stop()


def test_selection_stays_with_healthy_member_and_is_shared_by_all_service_ports():
    health = {"192.0.2.1": True, "192.0.2.2": True}
    calls = []

    def probe(endpoint):
        calls.append(endpoint.host)
        ready = health[endpoint.host]
        return ProbeResult(ready, ready)

    monitor = EndpointMonitor("coordinator", _config("coordinator"), probe=probe)
    monitor.start()
    try:
        _wait_until(lambda: all(state.eligible for state in monitor.states()))
        first = monitor.select("management")
        _wait_until(lambda: calls.count(first.host) >= 3)
        assert monitor.select("management").host == first.host
        health[first.host] = False
        _wait_until(lambda: not monitor.states()[0].eligible)
        second = monitor.select("management")
        assert second.host == "192.0.2.2"
        health[first.host] = True
        _wait_until(lambda: monitor.states()[0].eligible)
        assert monitor.select("management").host == second.host
        assert monitor.select("observability").address == second.host + ":1027"
    finally:
        monitor.stop()


def test_coordinator_members_include_standby_and_unready_primary_for_topology():
    results = {"192.0.2.1": ProbeResult(False, False), "192.0.2.2": ProbeResult(True, False)}
    monitor = EndpointMonitor("coordinator", _config("coordinator"), probe=lambda endpoint: results[endpoint.host])
    monitor.start()
    try:
        _wait_until(lambda: monitor.states()[1].eligible)
        assert len(monitor.members) == len(monitor.states()) == 2
        assert monitor.states()[0].snapshot("management").address == "192.0.2.1:1026"
        assert monitor.select("management").host == "192.0.2.2"
        assert not monitor.select("management").ready
        results["192.0.2.2"] = ProbeResult(True, True)
        _wait_until(lambda: monitor.states()[1].ready)
        assert monitor.select("management").address == "192.0.2.2:1026"
        assert monitor.select("management").ready
    finally:
        monitor.stop()


def test_stale_success_is_unavailable_without_synchronous_refresh():
    clock = [10.0]
    probe = Mock(return_value=ProbeResult(True, True))
    monitor = EndpointMonitor("controller", _config(count=1, interval=100), probe=probe, clock=lambda: clock[0])
    monitor.start()
    try:
        _wait_until(lambda: monitor.states()[0].eligible)
        assert monitor.select("api").port == 2026
        clock[0] = 20.0
        with pytest.raises(DiscoveryUnavailable):
            monitor.select("api")
        assert not monitor.states()[0].eligible
        assert probe.call_count == 1
    finally:
        monitor.stop()
