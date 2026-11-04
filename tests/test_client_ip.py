"""
Client identity tests.
Limits count IPv4 clients per address and IPv6 clients per /64 network, so
rotating IPv6 privacy addresses cannot reset a quota. See helvetra/infra#22.
"""

from types import SimpleNamespace

import pytest

from app.api.dependencies import client_network, get_client_ip


@pytest.mark.parametrize("address,expected", [
    ("85.195.238.74", "85.195.238.74"),
    ("2001:1600:18:103:a1b2:c3d4:e5f6:7788", "2001:1600:18:103::/64"),
    ("2001:1600:18:103::4", "2001:1600:18:103::/64"),
    ("::ffff:85.195.238.74", "85.195.238.74"),  # IPv4-mapped IPv6
    ("unknown", "unknown"),
])
def test_client_network(address, expected):
    assert client_network(address) == expected


def test_two_addresses_in_one_ipv6_network_are_one_client():
    """A device that rotates its IPv6 address keeps the same identity."""
    first = SimpleNamespace(headers={"X-Real-IP": "2a02:aa10:1:2::1"}, client=None)
    second = SimpleNamespace(headers={"X-Real-IP": "2a02:aa10:1:2:dead:beef:0:9"}, client=None)
    assert get_client_ip(first) == get_client_ip(second) == "2a02:aa10:1:2::/64"


def test_falls_back_to_the_connection_address():
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    assert get_client_ip(request) == "127.0.0.1"
