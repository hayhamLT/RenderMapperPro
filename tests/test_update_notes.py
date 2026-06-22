"""The updater's release-notes cleaner keeps the changelog, drops boilerplate.

The in-app update dialog must show the human changelog, not the generic
install-instructions table our release template leads with.
"""
from __future__ import annotations

from app_window.update_mixin import _clean_release_notes

_BODY = """Standalone builds of **Render Mapper Pro**.

| Platform | Installer (recommended) | Portable |
|----------|-------------------------|----------|
| Windows (x64) | `Setup.exe` | `zip` |

**Windows:** run the **Setup.exe**...
**macOS:** open the **.dmg**...
**Verify your download:** `SHA256SUMS.txt`...

## What's Changed
* feat: reimagine render settings by @hayhamLT in #65
* build: slim the bundle by @hayhamLT in #69

**Full Changelog**: https://github.com/x/y/compare/v1.8.20...v1.8.21
"""


def test_keeps_changelog_drops_boilerplate_and_footer():
    out = _clean_release_notes(_BODY)
    assert out.startswith("## What's Changed")
    assert "reimagine render settings" in out      # changelog kept
    assert "Platform" not in out and "Setup.exe" not in out   # install table gone
    assert "Full Changelog" not in out             # compare-link footer gone


def test_boilerplate_only_returns_empty():
    # No auto-generated changelog → nothing human to show → dialog hides the box.
    assert _clean_release_notes("Standalone builds...\n| Platform | x |\n**macOS:** ...") == ""
    assert _clean_release_notes("") == ""
