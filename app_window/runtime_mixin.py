"""Managed-Blender runtime install — the one-time download/extract of a Blender
runtime when none is found, its progress dialog, and the window methods that
drive it. Extracted verbatim from BlenderVideoMapperQt (the mixin operates on
``self``). The path/version logic lives in core.runtime (UI-free, shared with
the discovery path)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from app_window.base import _WindowMembers
from core.runtime import (
    BLENDER_RUNTIME_VERSION,
    RUNTIME_ROOT,
    _managed_blender_executable,
    _runtime_checksum_url,
    _runtime_download_spec,
)


class RuntimeInstallThread(QThread):
    log = Signal(str)
    progress = Signal(int, int)            # (bytes_read, bytes_total); total 0 = unknown
    finished_install = Signal(str, str)    # (path, error); error == "cancelled" when aborted

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cancelled = False

    def cancel(self) -> None:
        """Request abort; the in-flight download stops at its next chunk."""
        self._cancelled = True

    def _download(self, url: str, dest: Path) -> None:
        from core.download import download_with_progress
        self.log.emit(f"[runtime] Downloading {url}")
        self.log.emit("[runtime] This is a ~300–700 MB download and can take several minutes.")
        download_with_progress(url, dest, self.log.emit,
                               on_progress=self.progress.emit,
                               should_cancel=lambda: self._cancelled)

    def _verify_download(self, archive_path: Path, archive_name: str) -> None:
        """Verify the archive against blender.org's published SHA-256 sidecar
        before extracting (the same rigor the self-updater applies to installers).
        Fails CLOSED on a digest mismatch — deletes the bad archive and raises —
        but proceeds with a logged warning if the sidecar can't be fetched/parsed,
        so a transient network blip doesn't brick an otherwise-good install."""
        from core.archive import expected_sha256_from_sidecar, verify_sha256
        from core.download import fetch_text
        url = _runtime_checksum_url()
        if not url:
            return
        try:
            expected = expected_sha256_from_sidecar(fetch_text(url), archive_name)
        except Exception as exc:
            self.log.emit(f"[runtime] WARNING: couldn't fetch checksum sidecar ({exc}); skipping integrity check")
            return
        if not expected:
            self.log.emit(f"[runtime] WARNING: {archive_name} not listed in checksum sidecar; skipping integrity check")
            return
        self.log.emit("[runtime] Verifying download integrity (SHA-256)…")
        try:
            verify_sha256(archive_path, expected)
        except Exception:
            archive_path.unlink(missing_ok=True)   # never extract or reuse a bad archive
            raise
        self.log.emit("[runtime] Integrity verified ✓")

    def _extract_archive(self, archive_path: Path, staging_dir: Path) -> None:
        from core.archive import safe_extract_tar, safe_extract_zip
        name = archive_path.name.lower()
        if name.endswith(".zip"):
            safe_extract_zip(archive_path, staging_dir)   # rejects Zip-Slip members
            return
        if name.endswith(".tar.xz"):
            safe_extract_tar(archive_path, staging_dir)   # 3.12 'data' filter
            return
        if name.endswith(".dmg") and sys.platform == "darwin":
            # Try hdiutil mount/copy; fall back to treating as zip if unavailable
            if not shutil.which("hdiutil"):
                raise RuntimeError("hdiutil not found — cannot mount .dmg. Try installing via Homebrew or download a .zip build.")
            mount = ""
            try:
                result = subprocess.run(
                    ["hdiutil", "attach", "-nobrowse", "-readonly", str(archive_path)],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"hdiutil attach failed: {result.stderr.strip()}")
                for ln in result.stdout.splitlines():
                    if "/Volumes/" in ln:
                        parts = ln.split("\t")
                        mount = parts[-1].strip() if parts else ""
                if not mount:
                    raise RuntimeError("Could not determine mount point from hdiutil output")

                apps = sorted(Path(mount).glob("*.app"))
                if not apps:
                    raise RuntimeError("Blender.app not found in downloaded disk image")
                self.log.emit(f"[runtime] Copying {apps[0].name} from mounted image")
                shutil.copytree(apps[0], staging_dir / "Blender.app", dirs_exist_ok=True)
            finally:
                if mount:
                    subprocess.run(["hdiutil", "detach", mount, "-force"],
                                   capture_output=True, timeout=30)
            return
        raise RuntimeError(f"Unsupported runtime archive format: {archive_path.name}")

    def _locate_executable(self, root: Path) -> str:
        if sys.platform == "darwin":
            cands = [
                root / "Blender.app" / "Contents" / "MacOS" / "Blender",
            ]
            for c in cands:
                if c.exists() and c.is_file():
                    return str(c)
            for c in root.rglob("Blender.app"):
                exe = c / "Contents" / "MacOS" / "Blender"
                if exe.exists() and exe.is_file():
                    return str(exe)
        elif os.name == "nt":
            for c in root.rglob("blender.exe"):
                if c.exists() and c.is_file():
                    return str(c)
        else:
            for c in root.rglob("blender"):
                if c.exists() and c.is_file() and os.access(str(c), os.X_OK):
                    return str(c)
        raise RuntimeError("Installed runtime executable not found")

    def run(self) -> None:
        from core.download import DownloadCancelled
        try:
            spec = _runtime_download_spec()
            if not spec:
                raise RuntimeError("Managed runtime is not supported on this OS")
            url, archive_name = spec

            RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
            downloads = RUNTIME_ROOT / "downloads"
            downloads.mkdir(parents=True, exist_ok=True)

            archive_path = downloads / archive_name
            if not archive_path.exists() or archive_path.stat().st_size == 0:
                self._download(url, archive_path)
            else:
                self.log.emit("[runtime] Using cached runtime archive")
            if self._cancelled:
                raise DownloadCancelled()

            self._verify_download(archive_path, archive_name)

            with tempfile.TemporaryDirectory(prefix="blender-runtime-") as td:
                staging_dir = Path(td) / "staging"
                staging_dir.mkdir(parents=True, exist_ok=True)
                self.log.emit("[runtime] Installing runtime files")
                self._extract_archive(archive_path, staging_dir)

                exe = self._locate_executable(staging_dir)
                exe_path = Path(exe)

                final_dir = RUNTIME_ROOT / "current"
                old_dir = RUNTIME_ROOT / "previous"
                tmp_dir = RUNTIME_ROOT / ".next"
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)

                root_source = staging_dir
                if exe_path.is_relative_to(staging_dir):
                    parts = exe_path.relative_to(staging_dir).parts
                    if "Blender.app" in parts:
                        root_source = staging_dir / "Blender.app"
                shutil.copytree(root_source, tmp_dir, dirs_exist_ok=True)

                if old_dir.exists():
                    shutil.rmtree(old_dir, ignore_errors=True)
                if final_dir.exists():
                    final_dir.rename(old_dir)
                tmp_dir.rename(final_dir)

            installed = _managed_blender_executable()
            if not installed:
                raise RuntimeError("Runtime install completed but executable was not detected")
            self.finished_install.emit(installed, "")
        except DownloadCancelled:
            self.finished_install.emit("", "cancelled")
        except Exception as exc:
            self.finished_install.emit("", str(exc))


class _RuntimeProgressDialog(QDialog):
    """Modern progress UI for the one-time managed-Blender download (hundreds of
    MB): a live bar with MB / speed / ETA and a Cancel button. Non-modal, so the
    user can read Quick Start while it runs."""

    cancelled = Signal()

    def __init__(self, parent, version: str, palette) -> None:
        super().__init__(parent)
        self.setWindowTitle("Setting up Blender")
        self.setMinimumWidth(460)
        self._t0 = time.monotonic()
        self._done = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)
        title = QLabel(f"Setting up Blender {version}")
        title.setStyleSheet(f"color:{palette.text}; font-size:15px; font-weight:700;")
        sub = QLabel("Downloading a managed Blender runtime so you don't have to install "
                     "anything yourself. This happens once.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{palette.text_muted}; font-size:12px;")
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)            # indeterminate until the first byte lands
        self._bar.setTextVisible(False)
        self._status = QLabel("Starting…")
        self._status.setStyleSheet(f"color:{palette.text_muted}; font-size:12px;")
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self._cancel_btn)
        for w in (title, sub, self._bar, self._status):
            lay.addWidget(w)
        lay.addLayout(row)

    def _on_cancel(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status.setText("Cancelling…")
        self.cancelled.emit()

    def set_progress(self, read: int, total: int) -> None:
        if self._done:
            return
        mb = 1024 * 1024
        if total > 0 and read >= total:
            self._bar.setRange(0, 0)         # busy spinner for the extract/install phase
            self._status.setText("Installing…")
            return
        if total > 0:
            self._bar.setRange(0, 100)
            self._bar.setValue(int(read / total * 100))
            elapsed = max(0.001, time.monotonic() - self._t0)
            speed = read / elapsed
            eta = int((total - read) / speed) if speed > 0 else 0
            self._status.setText(
                f"Downloading… {read // mb} / {total // mb} MB · "
                f"{speed / mb:.1f} MB/s · ~{eta}s left")
        else:
            self._bar.setRange(0, 0)
            self._status.setText(f"Downloading… {read // mb} MB")

    def finish(self) -> None:
        self._done = True
        self.close()


class RuntimeMixin(_WindowMembers):

    def _prompt_install_runtime(self) -> None:
        if self._runtime_prompted:
            return
        self._runtime_prompted = True
        ans = QMessageBox.question(
            self,
            "Install Blender Runtime",
            "No Blender installation was found.\n"
            "Would you like to download and install a managed Blender runtime now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._install_managed_runtime()

    def _install_managed_runtime(self) -> None:
        if self._runtime_install_thread and self._runtime_install_thread.isRunning():
            dlg = getattr(self, "_runtime_progress_dialog", None)
            if dlg is not None:
                dlg.show()
                dlg.raise_()
            return
        if _runtime_download_spec() is None:
            QMessageBox.warning(
                self, "Automatic Setup Unavailable",
                "Automatic Blender download isn't supported on this OS. Please install "
                "Blender and point the app at it in Properties → Render Engines.")
            return

        self._append_log(f"[runtime] Installing Blender {BLENDER_RUNTIME_VERSION}...")
        thread = RuntimeInstallThread(self)
        self._runtime_install_thread = thread
        dlg = _RuntimeProgressDialog(self, BLENDER_RUNTIME_VERSION, self._palette)
        self._runtime_progress_dialog = dlg
        thread.progress.connect(dlg.set_progress)
        thread.log.connect(self._append_log)
        thread.finished_install.connect(self._on_runtime_installed)
        dlg.cancelled.connect(thread.cancel)
        thread.start()
        dlg.show()

    def _on_runtime_installed(self, blender_path: str, error: str) -> None:
        dlg = getattr(self, "_runtime_progress_dialog", None)
        if dlg is not None:
            dlg.finish()
            self._runtime_progress_dialog = None
        if error == "cancelled":
            self._append_log("[runtime] Blender setup cancelled.")
            return
        if error:
            self._append_log(f"[runtime] Install failed: {error}")
            QMessageBox.warning(
                self, "Blender Setup Failed",
                f"{error}\n\nYou can retry, or install Blender yourself and point the "
                f"app at it in Properties → Render Engines.")
            return
        self._blender_path = blender_path
        self._append_log(f"[runtime] Installed Blender runtime: {blender_path}")
        self._schedule_save()
        self._show_toast("Blender is ready — the app is fully set up", "success")
