"""The on-demand Playwright node driver resolves a correct download URL.

The frozen app drops Playwright's 114 MB ``node`` and fetches it on first web
render; these guard the URL/platform helpers (network-free) so a bad token or
URL shape is caught in CI rather than at a user's first render.
"""
from __future__ import annotations

import re

import pytest

from core import web_render as w


def test_driver_platform_token():
    tok = w._web_driver_platform()
    assert tok in {"mac-arm64", "mac", "win32_x64", "linux", "linux-arm64"}


def test_driver_url_shape():
    url = w._web_driver_url()
    assert url is not None
    assert url.startswith("https://")
    assert re.search(r"/playwright-\d+\.\d+\.\d+-[\w-]+\.zip$", url), url
    assert w._web_driver_platform() in url


def test_node_present_in_dev_tree():
    # Needs the playwright package installed (the lint-test CI job doesn't install
    # it — only the web-smoke job does). Where it IS present, node ships with it,
    # so ensure_web_node is a no-op from source.
    pytest.importorskip("playwright")
    assert w.web_node_installed() is True
    assert w._node_in_package() is not None
