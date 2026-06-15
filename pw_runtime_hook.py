"""PyInstaller runtime hook for the web render backend (frozen app only).

Runs before any app code, so it's the earliest place to:
1. Ensure Playwright's bundled ``node`` driver is executable. It ships via
   ``binaries=`` (which preserves the +x bit), so this is belt-and-suspenders.
2. Hard-set ``PLAYWRIGHT_BROWSERS_PATH`` to a writable per-user dir before any
   Playwright import — frozen Playwright otherwise ``setdefault``s it to "0",
   pointing the browser at the read-only app bundle. Must match
   ``core.web_render.WEB_RUNTIME_ROOT``.
"""
import os
import stat
import sys

if getattr(sys, "frozen", False):
    _base = getattr(sys, "_MEIPASS", "")
    _node = os.path.join(_base, "playwright", "driver",
                         "node.exe" if sys.platform == "win32" else "node")
    if os.path.exists(_node):
        try:
            os.chmod(_node, os.stat(_node).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.path.expanduser("~"), ".blender_video_mapper", "browsers")
