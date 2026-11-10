"""Every automated test is offline; accidental live calls fail immediately."""

import socket

import pytest


@pytest.fixture(autouse=True)
def forbid_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network access is forbidden in the automated suite")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
