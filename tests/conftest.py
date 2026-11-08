"""Test-suite wide fixtures (lives in tests/ so plain `pytest` picks it up)."""

from __future__ import annotations

import pytest


try:
    import pytest_socket

    _HAS_PYTEST_SOCKET = True
except ImportError:
    _HAS_PYTEST_SOCKET = False


if _HAS_PYTEST_SOCKET:

    @pytest.fixture(autouse=True)
    def _allow_loopback_sockets():
        """The fakes serve real HTTP on 127.0.0.1; unblock sockets.

        HA dev environments block all sockets by default (their plugin
        disables them around every test); the plain library environment
        has no pytest-socket and no blocking.
        """
        pytest_socket.enable_socket()
        yield
