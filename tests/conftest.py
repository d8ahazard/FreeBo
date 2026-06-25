"""Shared pytest fixtures + the --hardware gate.

The default suite is hardware-free: it exercises the safety floor, skills, perception, the diagnostics
check/motion logic (against a fake app client), and the brain's VLM reason path with a mocked vision service
and the MockRobotLink. Tests marked `@pytest.mark.hardware` only run with `pytest --hardware` against a live
app — they drive the real robot through the same self-test harness.
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# agent_next_2 §6/§12 — root-cause fix for the intermittent full-suite hang on Windows. pytest-asyncio creates a
# FRESH proactor event loop per async test; each loop builds a self-pipe via socket.socketpair(), whose stdlib
# implementation calls an UNBOUNDED lsock.accept() on a loopback listener. Under load that accept() occasionally
# blocks forever (a dropped loopback SYN), wedging the whole run at teardown. We replace socketpair with a
# bounded, retrying implementation so a flaked handshake retries instead of hanging. POSIX uses AF_UNIX and is
# unaffected, so this only patches win32.
if sys.platform.startswith("win"):
    def _robust_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
        last_err: Exception | None = None
        for _ in range(50):
            lsock = socket.socket(family, type, proto)
            csock = None
            try:
                lsock.bind((host, 0))
                lsock.listen()
                lsock.settimeout(0.5)                     # BOUND the accept so a lost SYN can't hang forever
                addr = lsock.getsockname()
                csock = socket.socket(family, type, proto)
                csock.settimeout(0.5)
                try:
                    csock.connect(addr)
                except (BlockingIOError, InterruptedError, OSError):
                    pass
                ssock, _ = lsock.accept()
                csock.setblocking(True)
                ssock.setblocking(True)
                lsock.close()
                return ssock, csock
            except (socket.timeout, OSError) as e:        # flaked handshake -> clean up + retry
                last_err = e
                try:
                    lsock.close()
                except Exception:  # noqa: BLE001
                    pass
                if csock is not None:
                    try:
                        csock.close()
                    except Exception:  # noqa: BLE001
                        pass
        raise OSError(f"socketpair failed after retries: {last_err}")

    socket.socketpair = _robust_socketpair  # type: ignore[assignment]


def pytest_addoption(parser):
    parser.addoption("--hardware", action="store_true", default=False,
                     help="run live-robot tests against a running app (AUTOBOT_APP_URL or --app-url)")
    parser.addoption("--app-url", action="store", default=os.environ.get("AUTOBOT_APP_URL",
                                                                         "http://127.0.0.1:8200"),
                     help="base URL of the running app for hardware tests")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--hardware"):
        return
    skip = pytest.mark.skip(reason="needs --hardware + a running app")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def app_url(request) -> str:
    return request.config.getoption("--app-url")


def settings(**changes):
    """A fresh Settings object with the given UI-editable overrides applied."""
    from autobot.config import Settings
    s = Settings()
    if changes:
        s.update(**changes)
    return s


@pytest.fixture
def mock_link():
    from autobot.robot.mock_link import MockRobotLink
    return MockRobotLink()
