"""HTTP download with progress reporting — UI-free, unit-testable.

Extracted from the Qt RuntimeInstallThread so the networking lives in core/ (not
on a widget/thread) and can be tested. Reports progress through an ``on_log``
callback instead of Qt signals; the QThread wrapper passes ``self.log.emit``.
"""
from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

LogCallback = Callable[[str], None]


def download_with_progress(url: str, dest: str | Path, on_log: LogCallback | None = None,
                           *, user_agent: str = "RenderMapperPro/1.0", timeout: int = 120,
                           chunk: int = 512 * 1024, tag: str = "[runtime]") -> int:
    """Stream ``url`` to ``dest``, logging percent / speed / ETA every ~5%.
    Returns the number of bytes written."""
    log = on_log or (lambda *_: None)
    dest = Path(dest)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length", "0") or "0")
        read = 0
        last_pct = -5
        t0 = time.monotonic()
        mb = 1024 * 1024
        while True:
            data = resp.read(chunk)
            if not data:
                break
            out.write(data)
            read += len(data)
            if total > 0:
                pct = int((read / total) * 100)
                if pct >= last_pct + 5:        # 5% steps, not one line per chunk
                    last_pct = pct
                    elapsed = max(0.001, time.monotonic() - t0)
                    speed = read / elapsed     # bytes/s
                    eta = (total - read) / speed if speed > 0 else 0
                    log(f"{tag} Download {pct}% — {read // mb}/{total // mb} MB "
                        f"· {speed / mb:.1f} MB/s · ~{int(eta)}s left")
    return read
