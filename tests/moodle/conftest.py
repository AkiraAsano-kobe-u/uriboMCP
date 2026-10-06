import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Moodle tests must never reach the real LMS (account lockout risk)."""

    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
