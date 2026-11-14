"""The suite's guard (conftest.no_live_network): no test reaches a real service."""

from __future__ import annotations

import socket

import httpx
import pytest

from conftest import LiveNetworkRefused


def test_a_tcp_connection_is_refused() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(LiveNetworkRefused):
            s.connect(("127.0.0.1", 8082))
        with pytest.raises(LiveNetworkRefused):
            s.connect_ex(("127.0.0.1", 8082))
    finally:
        s.close()


def test_an_unstubbed_http_call_never_leaves_the_process() -> None:
    with pytest.raises(httpx.HTTPError) as e:
        httpx.get("http://localhost:8082/v1/nodes", timeout=2)
    assert "stub the call with respx" in str(e.value)
