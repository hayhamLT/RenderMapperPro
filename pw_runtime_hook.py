"""PyInstaller runtime hook for the web render backend (frozen app only).

Runs before any app code, so it's the earliest place to hard-set
``PLAYWRIGHT_BROWSERS_PATH`` to a writable per-user dir before any Playwright
import — frozen Playwright otherwise ``setdefault``s it to "0", pointing the
browser at the read-only app bundle. Must match
``core.web_render.WEB_RUNTIME_ROOT``.

The ~114 MB Playwright ``node`` driver is NOT bundled; it's downloaded on first
web render (``core.web_render.ensure_web_node``), which also sets
``PLAYWRIGHT_NODEJS_PATH``. So there's nothing to make executable here anymore.
"""
import os
import sys

if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.path.expanduser("~"), ".blender_video_mapper", "browsers")
