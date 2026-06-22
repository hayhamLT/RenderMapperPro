#!/usr/bin/env python3
"""Vendor the three.js files the web-render scene needs into assets/vendor/three.

The headless three.js backend (assets/web_scene.html) must not depend on a live
CDN at render time — offline machines and farm/Deadline nodes would stall. This
downloads three's core module plus the addons web_scene.html imports (GLTFLoader,
RoomEnvironment) and recursively every addon they import, so the whole graph is
bundled by PyInstaller (assets/ ships verbatim).

Run after bumping THREE_VERSION (keep it in step with the importmap version
comment in web_scene.html):

    python tools/vendor_three.py
"""
from __future__ import annotations

import os
import pathlib
import re
import urllib.request

THREE_VERSION = "0.184.0"
BASE = f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}"
VENDOR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "vendor" / "three"
# Entry points: the core module (a thin re-export that pulls in three.core.js
# etc.) plus the addons web_scene.html imports. Their transitive imports are
# resolved and vendored automatically.
ENTRIES = [
    "build/three.module.js",
    "examples/jsm/loaders/GLTFLoader.js",
    "examples/jsm/environments/RoomEnvironment.js",
]
_IMPORT_RE = re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]""")


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def _local_for(cdn_rel: str) -> pathlib.Path:
    """Mirror the CDN layout under vendor/three: build/* lands flat (so the
    importmap's 'three' -> three.module.js and its ./three.core.js sibling
    resolve), examples/jsm/* lands under addons/ (matching the importmap prefix)."""
    if cdn_rel.startswith("build/"):
        return VENDOR / cdn_rel[len("build/"):]
    if cdn_rel.startswith("examples/jsm/"):
        return VENDOR / "addons" / cdn_rel[len("examples/jsm/"):]
    return VENDOR / cdn_rel


def main() -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    queue = list(ENTRIES)
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        src = _fetch(f"{BASE}/{rel}")
        dest = _local_for(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src, encoding="utf-8")
        for spec in _IMPORT_RE.findall(src):
            if spec == "three":
                continue                                  # bare → importmap → three.module.js
            if spec.startswith("three/addons/"):
                queue.append("examples/jsm/" + spec[len("three/addons/"):])
            elif spec.startswith((".", "..")):
                queue.append(os.path.normpath(os.path.join(os.path.dirname(rel), spec)))
    print(f"vendored {len(seen)} file(s):")
    for s in sorted(seen):
        print("  ", s)


if __name__ == "__main__":
    main()
