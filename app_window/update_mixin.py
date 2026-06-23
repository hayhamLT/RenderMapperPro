"""Self-updater — check GitHub Releases, offer an update, download + integrity-
verify the platform installer, and hand off to it. Extracted verbatim from
BlenderVideoMapperQt (operates on ``self``). The ``_update_checked`` Signal stays
on the window (Qt Signals must live on the QObject)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from app_version import APP_NAME
from app_version import __version__ as APP_VERSION
from app_window.base import _WindowMembers
from core.utils import bundled_asset_path
from core.utils import ssl_context as _ssl_context
from core.utils import update_platform_key as _update_platform_key
from core.utils import version_tuple as _version_tuple
from media import reveal_in_file_manager
from workers import FuncThread

GITHUB_REPO = "hayhamLT/RenderMapperPro"   # for the auto-updater


def _clean_release_notes(body: str) -> str:
    """Pull just the human changelog out of a GitHub release body for the in-app
    updater. Our release template leads with a generic install-instructions table
    (platform / installer / portable + verify notes) that's meaningless inside
    the app itself — strip it, keeping GitHub's auto-generated 'What's Changed'.
    Returns '' when there's nothing worth showing, so the dialog hides the box."""
    if not body:
        return ""
    low = body.lower()
    for marker in ("## what's changed", "## what’s changed", "## what's new", "## changelog"):
        i = low.find(marker)
        if i != -1:
            notes = body[i:].strip()
            fc = notes.lower().rfind("\n**full changelog**")   # drop the compare-link footer
            return notes[:fc].strip() if fc != -1 else notes
    return ""


def _update_token() -> str:
    """Optional GitHub token for the auto-updater. The repo is public, so updates
    work with NO token (anonymous API); a token only raises the rate limit. Read
    from the RMP_UPDATE_TOKEN env var (dev convenience) — shipped builds bake in
    no token on purpose, since a credential in a public binary is extractable."""
    t = os.environ.get("RMP_UPDATE_TOKEN", "").strip()
    if t:
        return t
    f = bundled_asset_path("update_token.txt")
    if f is not None:
        try:
            return f.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


class UpdateMixin(_WindowMembers):

    # In-app update downloads the platform INSTALLER and launches it — the
    # installer handles replacing the (possibly running) app, so there's no
    # extract-over-a-locked-exe problem. (Releases also ship portable .zips.)
    # No macos-intel entry: releases are Apple-Silicon-only (the deprecated Intel
    # runner build was dropped). An Intel Mac would get "no installer for your
    # platform", which is correct — there's nothing to hand it.
    _ASSET_FOR_PLATFORM: ClassVar = {
        "macos-arm64": "RenderMapperPro-macOS-arm64.dmg",
        "windows-x64": "RenderMapperPro-Windows-x64-Setup.exe",
    }

    def _check_for_updates(self, manual: bool = False) -> None:
        # The repo is public, so update checks work anonymously. A token (dev env
        # var only — never baked into shipped builds) just raises the rate limit.
        token = _update_token()
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

        ctx = _ssl_context()

        def _fetch(use_token: bool):
            headers = {"Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28",
                       "User-Agent": APP_NAME}
            if use_token and token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))

        def work():
            info = None
            err = ""
            try:
                info = _fetch(use_token=bool(token))
            except Exception:
                # A token may be revoked or rate-limited — fall back to the
                # anonymous public API so updates never break on a credential.
                try:
                    info = _fetch(use_token=False)
                except Exception as exc2:
                    info = None
                    err = f"{type(exc2).__name__}: {exc2}"
            self._update_checked.emit(info, manual, err)

        self._update_check_thread = FuncThread(work)
        self._update_check_thread.start()

    def _launch_update_check(self) -> None:
        """The startup update check — skipped entirely if the user turned off the
        on-launch check in Properties → Updates."""
        if self._check_updates_on_launch:
            self._check_for_updates(manual=False)

    def _on_update_checked(self, info, manual: bool, error: str = "") -> None:
        if self._shutting_down:
            return
        if not info:
            # Surface the real reason — a frozen build hitting a TLS/cert problem,
            # a rate-limit (HTTP 403), DNS, a proxy, etc. — instead of a generic
            # "couldn't reach GitHub" that hides what actually went wrong.
            if error:
                self._append_log(f"[update] Check failed — {error}")
            if manual:
                detail = f"\n\n{error}" if error else ""
                QMessageBox.warning(self, "Updates",
                    "Couldn't reach GitHub to check for updates." + detail
                    + "\n\nYou can always download the latest version from the "
                    "Releases page on GitHub.")
            return
        tag = str(info.get("tag_name", "")).strip()
        if not tag or _version_tuple(tag) <= _version_tuple(APP_VERSION):
            self._sb_update.setText("")
            if manual:
                QMessageBox.information(self, "Updates", f"You're up to date (v{APP_VERSION}).")
            return
        self._sb_update.setText(f"● Update {tag} available")
        # A version the user chose to skip stays quiet on the automatic launch
        # check — the status-bar badge still shows it, and a manual "Check Now"
        # (or the next, newer release) always pops the dialog.
        if not manual and tag == self._skipped_update:
            return
        self._offer_update(info, tag)

    def _offer_update(self, info, tag: str) -> None:
        """A themed, platform-aware 'update available' card. macOS and Windows get
        their own install wording and primary-button label so each feels native."""
        pal = self._palette
        is_mac = sys.platform == "darwin"
        is_win = sys.platform == "win32"

        dlg = QDialog(self)
        dlg.setWindowTitle("Software Update")
        dlg.setMinimumWidth(420)
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(26, 24, 26, 18)
        outer.setSpacing(0)

        # ── Header: app icon + a cute version transition ────────────────
        head = QHBoxLayout()
        head.setSpacing(15)
        icon_lbl = QLabel()
        pm = self.windowIcon().pixmap(54, 54)
        if not pm.isNull():
            icon_lbl.setPixmap(pm)
        icon_lbl.setFixedSize(54, 54)
        head.addWidget(icon_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        head_text = QVBoxLayout()
        head_text.setSpacing(3)
        title = QLabel("A new version is ready")
        title.setStyleSheet(f"color:{pal.text}; font-size:17px; font-weight:600;")
        title.setWordWrap(True)
        # v1.8.20 → v1.8.21, the new one in the brand accent.
        vt = QLabel(f"v{APP_VERSION}&nbsp;&nbsp;&#8594;&nbsp;&nbsp;"
                    f"<span style='color:{pal.accent}; font-weight:600;'>{tag}</span>")
        vt.setTextFormat(Qt.TextFormat.RichText)
        vt.setStyleSheet(f"color:{pal.text_muted}; font-size:13px;")
        head_text.addWidget(title)
        head_text.addWidget(vt)
        head.addLayout(head_text, 1)
        outer.addLayout(head)
        outer.addSpacing(16)

        # ── What's new — the real changelog only (install boilerplate
        #    stripped); the whole box is hidden when there's nothing human. ──
        notes = _clean_release_notes(str(info.get("body") or ""))
        if notes:
            label = QLabel("What's new")
            label.setStyleSheet(f"color:{pal.text_faint}; font-size:10px; "
                                f"font-weight:600; letter-spacing:1.5px;")
            outer.addWidget(label)
            outer.addSpacing(5)
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            try:
                view.setMarkdown(notes)
            except Exception:
                view.setPlainText(notes)
            view.setStyleSheet(
                f"QTextBrowser{{background:{pal.surface}; border:none; "
                f"border-radius:10px; padding:10px 12px; color:{pal.text}; font-size:12px;}}")
            view.setMinimumHeight(96)
            view.setMaximumHeight(168)
            outer.addWidget(view)
            outer.addSpacing(14)

        # ── Platform-specific install note ──────────────────────────────
        if is_mac:
            note = (f"It'll download the macOS installer (.dmg) and open it — "
                    f"drag {APP_NAME} to Applications to finish.")
        elif is_win:
            note = (f"It'll download and run the Windows installer — your settings "
                    f"are kept and {APP_NAME} relaunches when it's done.")
        else:
            note = "It'll download the installer and reveal it in your file manager."
        note_lbl = QLabel(note)
        note_lbl.setStyleSheet(f"color:{pal.text_muted}; font-size:11px;")
        note_lbl.setWordWrap(True)
        outer.addWidget(note_lbl)
        outer.addSpacing(16)

        # ── Buttons (platform-native ordering + accent primary) ─────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        later = QPushButton("Later")
        later.setObjectName("SmallButton")
        primary_label = "Download Update" if is_mac else "Update Now"
        primary = QPushButton(primary_label)
        primary.setObjectName("PrimaryButton")
        primary.setStyleSheet(
            f"QPushButton#PrimaryButton{{background:{pal.accent}; color:{pal.accent_text}; "
            f"border:none; border-radius:7px; padding:8px 18px; font-weight:600;}}"
            f"QPushButton#PrimaryButton:hover{{background:{pal.accent_hover};}}")
        primary.setCursor(Qt.CursorShape.PointingHandCursor)
        later.setCursor(Qt.CursorShape.PointingHandCursor)
        # "Skip this version" — left-aligned and quiet, like Sparkle. Declining a
        # version stops the launch nag for it but not for the next release.
        skip = QPushButton("Skip this version")
        skip.setObjectName("LinkButton")
        skip.setStyleSheet(f"QPushButton#LinkButton{{background:transparent; border:none; "
                           f"color:{pal.text_faint}; padding:8px 4px;}}"
                           f"QPushButton#LinkButton:hover{{color:{pal.text_muted};}}")
        skip.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row.addWidget(skip)
        btn_row.addStretch(1)
        # macOS keeps the affirmative button rightmost; Windows too — same order here.
        btn_row.addWidget(later)
        btn_row.addWidget(primary)
        outer.addLayout(btn_row)

        # Custom codes so we can tell Skip apart from Later.
        _SKIP = 2
        primary.clicked.connect(dlg.accept)
        later.clicked.connect(dlg.reject)
        skip.clicked.connect(lambda: dlg.done(_SKIP))
        primary.setDefault(True)
        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted:
            self._fetch_update(info)
        elif result == _SKIP:
            self._skipped_update = tag
            self._append_log(f"[update] Skipping {tag} — won't prompt again until a newer release.")
            self._save_profile()

    def _fetch_update(self, info) -> None:
        want = self._ASSET_FOR_PLATFORM.get(_update_platform_key()) or ""
        asset = next((a for a in info.get("assets", []) if a.get("name") == want), None)
        if not asset:
            QMessageBox.warning(self, "Update",
                f"This release has no installer for your platform ({_update_platform_key()}).")
            return
        token = _update_token()
        tag = str(info.get("tag_name", "") or "update").lstrip("v") or "update"
        url = str(asset.get("url") or "")
        expected = str(asset.get("digest") or "").strip().lower()   # GitHub-published "sha256:<hex>"
        dest = Path.home() / "Downloads" / want
        tmp = dest.with_name(dest.name + ".part")
        try:
            import hashlib
            import hmac
            import urllib.parse
            import urllib.request
            headers = {"Accept": "application/octet-stream", "User-Agent": APP_NAME}
            # Host-pin the credential: only ever attach the token when the asset
            # URL is on GitHub's own hosts (defence against a redirected/tampered
            # URL leaking the token elsewhere).
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if token and (host == "api.github.com" or host.endswith(".github.com")
                          or host.endswith(".githubusercontent.com")):
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Download to a temp file while hashing — the final path only ever
            # holds a fully-downloaded, integrity-checked installer.
            h = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=300, context=_ssl_context()) as r, open(tmp, "wb") as f:
                for chunk in iter(lambda: r.read(1 << 16), b""):
                    h.update(chunk)
                    f.write(chunk)
            got = "sha256:" + h.hexdigest()
            # Verify against GitHub's authoritative digest. A mismatch means the
            # bytes were tampered with or corrupted in flight — refuse to run it.
            if expected and not hmac.compare_digest(got, expected):
                tmp.unlink(missing_ok=True)
                self._append_log(f"[update] Integrity check FAILED for {want}: "
                                 f"expected {expected}, got {got}")
                QMessageBox.critical(self, "Update Blocked",
                    "The downloaded installer failed its integrity check and was discarded — "
                    "it does not match the checksum published with the release.\n\n"
                    "Please download the update manually from the GitHub Releases page.")
                return
            if not expected:
                self._append_log(f"[update] No published checksum for {want} — integrity not verified.")
            os.replace(tmp, dest)
            # Hand off to the platform installer, then QUIT — the running app must
            # not be open while it's replaced (macOS can't drag over a running app;
            # Windows can't overwrite a running .exe).
            quit_after = sys.platform in ("win32", "darwin")
            if sys.platform == "win32":
                os.startfile(str(dest))                  # runs Setup.exe (wizard)
                note = (f"{APP_NAME} will now quit so the installer can replace it, "
                        f"then relaunches when you click Finish in the installer.")
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(dest)])    # mounts the .dmg
                note = (f"{APP_NAME} will now quit so you can drag the new version into "
                        f"Applications (you can't replace a running app), then reopen it.")
            else:
                reveal_in_file_manager(dest)
                note = "Run the downloaded installer to finish updating."
            QMessageBox.information(self, "Update Ready",
                f"{APP_NAME} {tag} downloaded and verified.\n\nThe installer is opening. {note}")
            if quit_after:
                self._append_log(f"[update] Quitting so the {tag} installer can replace the app.")
                self._shutting_down = True
                # Close cleanly (saves the profile, stops worker threads) — with
                # quitOnLastWindowClosed (default) this also exits the app.
                self.close()
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            QMessageBox.warning(self, "Update Failed", str(exc))
