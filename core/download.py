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
ProgressCallback = Callable[[int, int], None]   # (bytes_read, bytes_total); total 0 = unknown
CancelCheck = Callable[[], bool]


class DownloadCancelled(Exception):
    """Raised by :func:`download_with_progress` when ``should_cancel()`` returns True."""


def fetch_text(url: str, *, user_agent: str = "RenderMapperPro/1.0", timeout: int = 30) -> str:
    """GET a small text resource over the verifying SSL context (e.g. a checksum
    sidecar). Uses the same bundled-CA context as the big download."""
    from core.utils import ssl_context
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        return resp.read().decode("utf-8", "replace")


def download_with_progress(url: str, dest: str | Path, on_log: LogCallback | None = None,
                           *, on_progress: ProgressCallback | None = None,
                           should_cancel: CancelCheck | None = None,
                           user_agent: str = "RenderMapperPro/1.0", timeout: int = 120,
                           chunk: int = 512 * 1024, tag: str = "[runtime]") -> int:
    """Stream ``url`` to ``dest``, logging percent / speed / ETA every ~5%.

    ``on_progress(bytes_read, bytes_total)`` is called on every chunk so a UI can
    drive a real progress bar (``bytes_total`` is 0 when the server omits
    Content-Length). If ``should_cancel`` returns True mid-stream, the partial
    file is left in place and :class:`DownloadCancelled` is raised.
    Returns the number of bytes written."""
    log = on_log or (lambda *_: None)
    dest = Path(dest)
    from core.utils import ssl_context
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length", "0") or "0")
        read = 0
        last_pct = -5
        t0 = time.monotonic()
        mb = 1024 * 1024
        if on_progress is not None:
            on_progress(0, total)
        while True:
            if should_cancel is not None and should_cancel():
                raise DownloadCancelled()
            data = resp.read(chunk)
            if not data:
                break
            out.write(data)
            read += len(data)
            if on_progress is not None:
                on_progress(read, total)
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
