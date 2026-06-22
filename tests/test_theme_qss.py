"""The generated Qt stylesheet must be well-formed.

A QSS syntax error (e.g. an unterminated/stray comment) is NOT a crash — Qt's
parser is lenient and silently drops bad rules, so the app just renders
unstyled. Qt's own "Could not parse" warning is unreliable (it misses many
cases), so validate the QSS string structurally instead: balanced /* */
comments (the exact bug that once shipped an unstyled app) and balanced braces.
"""
from __future__ import annotations

import re

import pytest

import theme as T


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_stylesheet_well_formed(mode):
    qss = T.stylesheet(T.build_palette(mode, T.ACCENT_ORANGE))
    # Strip every well-formed /* ... */ comment; any leftover marker is a stray
    # `/*` or `*/` outside a comment — i.e. malformed (the bug that shipped once).
    stripped = re.sub(r"/\*.*?\*/", "", qss, flags=re.DOTALL)
    assert "/*" not in stripped and "*/" not in stripped, \
        f"unbalanced QSS comment in {mode} theme"
    # The rendered sheet uses single braces; they must balance.
    assert qss.count("{") == qss.count("}"), f"unbalanced QSS braces in {mode} theme"
