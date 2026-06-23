"""Shared pytest fixtures + the --hardware gate.

The default suite is hardware-free: it exercises the safety floor, skills, perception, the diagnostics
check/motion logic (against a fake app client), and the brain's VLM reason path with a mocked vision service
and the MockRobotLink. Tests marked `@pytest.mark.hardware` only run with `pytest --hardware` against a live
app — they drive the real robot through the same self-test harness.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
