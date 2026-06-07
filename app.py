from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import customtkinter as ctk

from core.discovery import discover_scene_elements
from core.models import (
    VIDEO_MAPPING_MODE_BASE_COLOR,
    VIDEO_MAPPING_MODE_EMISSION,
    JobConfig,
    MaterialVideoAssignment,
    RenderOptions,
)
from core.runner import run_blender_job
from core.utils import file_exists, resolve_output_path, slugify_filename

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_TK_DND = True
    # tkdnd is unstable with macOS system Tk in both source and frozen runs on
    # some environments; disable to keep app startup reliable.
    if sys.platform == "darwin":
        HAS_TK_DND = False
except ImportError:
    DND_FILES = "DND_Files"
    TkinterDnD = None
    HAS_TK_DND = False


FRAME_PATTERNS = [
    re.compile(r"Fra:(\d+)"),
    re.compile(r"Frame\s+(\d+)", flags=re.IGNORECASE),
]

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
OUTPUT_PROFILES: dict[str, tuple[str, str]] = {
    "H264 MP4": ("MPEG4", "H264"),
    "ProRes MOV": ("QUICKTIME", "PRORES"),
    "PNG Sequence": ("PNG", "NONE"),
    "OpenEXR Sequence": ("OPEN_EXR", "NONE"),
}
VIDEO_MAPPING_MODE_LABELS = {
    VIDEO_MAPPING_MODE_EMISSION: "Emission Full Bright",
    VIDEO_MAPPING_MODE_BASE_COLOR: "Base Color + Alpha",
}
DISCOVERY_DEFAULT_TIMEOUT_SECONDS = 600
RUNTIME_SCRIPT_ROOT_ENV = "BLENDER_VIDEO_MAPPER_SCRIPT_ROOT"


def _normalize_blender_executable(candidate: str) -> str | None:
    candidate = candidate.strip()
    if not candidate:
        return None

    expanded = Path(os.path.expanduser(candidate))

    if expanded.suffix.lower() == ".app":
        for bundle_candidate in (
            expanded / "Contents/MacOS/Blender",
            expanded / "Contents/MacOS/blender",
        ):
            if bundle_candidate.exists() and bundle_candidate.is_file():
                return str(bundle_candidate)

    if expanded.exists() and expanded.is_file():
        return str(expanded)

    resolved = shutil.which(candidate)
    if resolved:
        return resolved

    return None


def _find_blender_executable(preferred: str = "") -> str | None:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        value = (candidate or "").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    add(preferred)
    add(os.environ.get("BLENDER_PATH"))

    for command in ("blender", "Blender"):
        add(shutil.which(command))
        add(command)

    for root in (Path("/Applications"), Path.home() / "Applications"):
        if not root.exists():
            continue
        for bundle in sorted(root.glob("Blender*.app"), reverse=True):
            add(str(bundle))
            add(str(bundle / "Contents/MacOS/Blender"))
            add(str(bundle / "Contents/MacOS/blender"))

    add("/Applications/Blender.app")
    add("/Applications/Blender.app/Contents/MacOS/Blender")
    add("/Applications/Blender.app/Contents/MacOS/blender")
    add(str(Path.home() / "Applications/Blender.app"))
    add(str(Path.home() / "Applications/Blender.app/Contents/MacOS/Blender"))
    add(str(Path.home() / "Applications/Blender.app/Contents/MacOS/blender"))

    for candidate in candidates:
        resolved = _normalize_blender_executable(candidate)
        if resolved:
            return resolved

    return None


def _detect_system_theme() -> str:
    override = os.environ.get("BLENDER_VIDEO_MAPPER_THEME", "").strip().lower()
    if override in {"light", "dark"}:
        return override

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip().lower() == "dark":
                return "dark"
        except Exception:
            pass

    return "light"


def _theme_palette(theme_name: str) -> dict[str, str]:
    if theme_name == "dark":
        return {
            "root_bg": "#0f172a",
            "card_bg": "#111827",
            "card_border": "#334155",
            "text": "#e5e7eb",
            "heading": "#f8fafc",
            "subhead": "#cbd5e1",
            "hint": "#94a3b8",
            "tree_header_bg": "#1f2937",
            "tree_header_fg": "#e5e7eb",
            "input_bg": "#0b1220",
            "input_fg": "#e5e7eb",
            "input_border": "#475569",
            "selection_bg": "#2563eb",
            "selection_fg": "#ffffff",
            "log_bg": "#020617",
            "log_fg": "#e2e8f0",
            "log_insert": "#e2e8f0",
            "status_default_bg": "#1e293b",
            "status_default_fg": "#e2e8f0",
        }

    return {
        "root_bg": "#f4f6f8",
        "card_bg": "#ffffff",
        "card_border": "#d6dbe4",
        "text": "#253144",
        "heading": "#0f172a",
        "subhead": "#475569",
        "hint": "#64748b",
        "tree_header_bg": "#eef2f7",
        "tree_header_fg": "#1f2937",
        "input_bg": "#ffffff",
        "input_fg": "#111827",
        "input_border": "#cbd5e1",
        "selection_bg": "#2563eb",
        "selection_fg": "#ffffff",
        "log_bg": "#0f172a",
        "log_fg": "#e2e8f0",
        "log_insert": "#e2e8f0",
        "status_default_bg": "#e9eef5",
        "status_default_fg": "#334155",
    }


def _resolve_project_root_from_executable() -> Path | None:
    exe = Path(sys.executable).resolve()
    for parent in [exe.parent, *exe.parents]:
        candidate = parent / "app.py"
        if candidate.exists():
            return parent
    return None


def _iter_runtime_script_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def add(candidate: str | Path | None) -> None:
        if candidate is None:
            return

        try:
            path = Path(candidate).expanduser().resolve()
        except Exception:
            return

        key = str(path)
        if key in seen or not path.exists() or not path.is_dir():
            return

        seen.add(key)
        roots.append(path)

    add(os.environ.get(RUNTIME_SCRIPT_ROOT_ENV))

    if getattr(sys, "frozen", False):
        add(getattr(sys, "_MEIPASS", None))

    add(Path(__file__).resolve().parent)
    add(_resolve_project_root_from_executable())

    argv0 = (sys.argv[0] if sys.argv else "").strip()
    if argv0:
        add(Path(argv0).expanduser().resolve().parent)

    add(Path.cwd())
    return roots


def _resolve_runtime_script(script_name: str) -> str:
    checked_paths: list[str] = []

    for root in _iter_runtime_script_roots():
        candidate = root / script_name
        checked_paths.append(str(candidate))
        if candidate.exists() and candidate.is_file():
            return str(candidate)

    checked_display = ", ".join(checked_paths) if checked_paths else "<no candidate roots>"
    raise FileNotFoundError(f"Runtime helper script not found: {script_name}. Checked: {checked_display}")


def _resolve_fallback_python_bin() -> str | None:
    candidates = [
        str(Path.home() / ".local/bin/python3.12"),
        shutil.which("python3.12"),
        shutil.which("python3"),
        "/Applications/Xcode.app/Contents/Developer/usr/bin/python3",
        "/usr/bin/python3",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return None


def _launch_source_ui_from_bundle() -> tuple[bool, str]:
    project_root = _resolve_project_root_from_executable()
    if not project_root:
        return False, "Could not resolve project root from app bundle executable path."

    source_app = project_root / "app.py"
    if not source_app.exists():
        return False, f"Source app not found: {source_app}"

    python_bin = _resolve_fallback_python_bin()
    if not python_bin:
        return False, "No usable python3 executable found."

    log_path = Path.home() / ".blender_video_mapper" / "logs" / "fallback_launch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    child_env = os.environ.copy()
    script_root = getattr(sys, "_MEIPASS", None)
    if script_root:
        child_env[RUNTIME_SCRIPT_ROOT_ENV] = str(Path(script_root).resolve())
    log_file.write(
        f"[{datetime.now().isoformat(timespec='seconds')}] Launcher mode start: {python_bin} {source_app}\n"
    )
    log_file.flush()

    try:
        subprocess.Popen(
            [python_bin, str(source_app)],
            cwd=str(project_root),
            env=child_env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    except Exception as exc:
        return False, str(exc)

    return True, "Source UI launched successfully."


@dataclass
class RenderJob:
    id: int
    video_path: str = ""
    label: str = ""
    name_seed: str = ""
    output_path: str = ""
    output_input: str = ""
    scene_path: str = ""
    target_camera: str = ""
    output_profile: str = "H264 MP4"
    render_options: RenderOptions | None = None
    retry_count: int = 0
    safe_mode: bool = True
    status: str = "idle"
    attempts: int = 0
    error: str = ""
    progress: float = 0.0
    logs: list[str] = field(default_factory=list)
    material_assignments: list[MaterialVideoAssignment] = field(default_factory=list)


# ── CTK-native replacement widgets ────────────────────────────────────────────

class _CtkList:
    """CTK scrollable multi-select list replacing tk.Listbox."""

    _SEL_TEXT = ("#1a7fd4", "#4da6ff")   # blue text when selected (light, dark)
    _DIM_TEXT = ("gray40", "gray65")       # normal label text
    _DROP = ("#1d4ed8", "#1e40af")
    _ROW = ("gray94", "gray18")
    _row_registry: dict[str, tuple["_CtkList", int]] = {}
    _active_drag: tuple["_CtkList", int, str] | None = None

    def __init__(self, parent: ctk.CTkFrame, height: int = 0,
                 font: ctk.CTkFont | None = None, detail_mode: str = "path",
                 single_select: bool = False) -> None:
        self._font = font
        # If height is 0, don't set fixed height (will expand dynamically)
        kwargs = {"fg_color": "transparent"}
        if height > 0:
            kwargs["height"] = height * 28
        self._sf = ctk.CTkScrollableFrame(parent, **kwargs)
        # Hide the scrollbar when not needed
        if hasattr(self._sf, "_scrollbar"):
            self._sf._scrollbar.grid_remove()
        self._items: list[str] = []
        self._selected: set[int] = set()
        self._callbacks: list = []
        self._last: int | None = None
        self._detail_mode = detail_mode
        self._single_select = single_select
        self._drop_handler = None
        self._drop_target_idx: int | None = None
        self._rows: list[ctk.CTkFrame] = []
        self._labels: dict[int, list[ctk.CTkLabel]] = {}  # idx -> label widgets
        self._tints: dict[int, tuple[str, str] | None] = {}  # idx -> (light_hex, dark_hex) row tint

    def pack(self, **kw) -> None: self._sf.pack(**kw)
    def configure(self, **kw) -> None: self._sf.configure(**kw)
    def drop_target_register(self, *_) -> None: pass
    def dnd_bind(self, *_) -> None: pass

    def set_drop_handler(self, handler) -> None:
        self._drop_handler = handler

    def set_indicator(self, idx: int, color: str | None) -> None:
        """Tint the entire row with a soft version of color. color=None to clear."""
        if idx >= len(self._rows):
            return
        self._tints[idx] = self._make_row_tint(color) if color else None
        self._repaint()

    @staticmethod
    def _make_row_tint(hex_color: str) -> tuple[str, str]:
        """Return (light_mode_hex, dark_mode_hex) for a subtle row background tint."""
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        # Light mode: blend toward gray94 (~240,240,240) at 22%
        lr = int(240 + (r - 240) * 0.22)
        lg = int(240 + (g - 240) * 0.22)
        lb = int(240 + (b - 240) * 0.22)
        # Dark mode: blend toward gray18 (~46,46,46) at 32%
        dr = int(46 + (r - 46) * 0.32)
        dg = int(46 + (g - 46) * 0.32)
        db = int(46 + (b - 46) * 0.32)
        return (f"#{lr:02x}{lg:02x}{lb:02x}", f"#{dr:02x}{dg:02x}{db:02x}")

    def _update_scrollbar(self) -> None:
        """Show scrollbar only when content overflows the visible area."""
        sf = self._sf
        if not hasattr(sf, "_scrollbar") or not hasattr(sf, "_parent_canvas"):
            return
        def _check() -> None:
            try:
                canvas = sf._parent_canvas
                canvas.update_idletasks()
                bbox = canvas.bbox("all")
                content_h = bbox[3] if bbox else 0
                view_h = canvas.winfo_height()
                if content_h > view_h:
                    sf._scrollbar.grid()
                else:
                    sf._scrollbar.grid_remove()
            except Exception:
                pass
        sf.after(50, _check)

    def delete(self, start, end=None) -> None:
        for w in self._sf.winfo_children():
            self._row_registry.pop(str(w), None)
            w.destroy()
        self._items.clear()
        self._selected.clear()
        self._last = None
        self._rows.clear()
        self._labels.clear()
        self._tints.clear()
        self._drop_target_idx = None
        self._update_scrollbar()

    def insert(self, _idx, text: str) -> None:
        i = len(self._items)
        self._items.append(text)
        row = ctk.CTkFrame(self._sf, fg_color=self._ROW, corner_radius=4, cursor="hand2")
        row.pack(fill="x", pady=0, padx=2)
        self._rows.append(row)
        self._tints[i] = None
        self._labels[i] = []
        self._row_registry[str(row)] = (self, i)

        widgets: list[ctk.CTkBaseClass] = [row]
        if self._detail_mode == "path":
            name_lbl = ctk.CTkLabel(row, text=Path(text).name, anchor="w", font=self._font)
            name_lbl.pack(fill="x", padx=8, pady=(3, 1))
            path_lbl = ctk.CTkLabel(
                row, text=text, anchor="w",
                font=ctk.CTkFont(size=9),
                text_color=("gray50", "gray55"),
            )
            path_lbl.pack(fill="x", padx=8, pady=(0, 3))
            self._labels[i] = [name_lbl]
            widgets.extend([name_lbl, path_lbl])
        else:
            # In text mode, show only filename but keep full path stored
            display_text = Path(text).name if '/' in text or '\\' in text else text
            label = ctk.CTkLabel(row, text=display_text, anchor="w", font=self._font)
            label.pack(side="left", fill="x", expand=True, padx=(8, 8), pady=4)
            self._labels[i] = [label]
            widgets.append(label)

        for w in widgets:
            w.bind("<Button-1>", lambda e, idx=i: self._click(idx, e))
            w.bind("<ButtonPress-1>", lambda e, idx=i: self._start_drag(idx, e), add="+")
            w.bind("<ButtonRelease-1>", lambda e: self._finish_drag(e), add="+")
            w.bind("<Enter>", lambda e, idx=i: self._on_enter(idx), add="+")
            w.bind("<Leave>", lambda e, idx=i: self._on_leave(idx), add="+")
        self._update_scrollbar()

    def curselection(self) -> tuple[int, ...]:
        return tuple(sorted(self._selected))

    def selected_value(self) -> str:
        selected = self.curselection()
        if len(selected) != 1:
            return ""
        idx = selected[0]
        return self._items[idx] if 0 <= idx < len(self._items) else ""

    def selection_set(self, idx: int) -> None:
        if 0 <= idx < len(self._items):
            self._selected = {idx}
            self._last = idx
            self._repaint()

    def bind(self, event: str, cb) -> None:
        if event == "<<ListboxSelect>>":
            self._callbacks.append(cb)

    def _click(self, idx: int, event) -> None:
        ctrl = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        if self._single_select:
            self._selected = {idx}
        elif ctrl:
            self._selected ^= {idx}
        elif shift and self._last is not None:
            lo, hi = sorted((self._last, idx))
            self._selected = set(range(lo, hi + 1))
        else:
            self._selected = {idx}
        self._last = idx
        self._repaint()
        for cb in self._callbacks:
            cb(None)

    def _start_drag(self, idx: int, _event) -> None:
        if 0 <= idx < len(self._items):
            self._active_drag = (self, idx, self._items[idx])

    def _finish_drag(self, _event) -> None:
        active = self._active_drag
        if active is None:
            return
        source_list, source_idx, payload = active
        target_idx = self._drop_target_idx
        self._active_drag = None
        source_list._clear_drop_targets()
        self._clear_drop_targets()
        if self._drop_handler is not None and target_idx is not None and source_list is not self:
            self._drop_handler(payload, target_idx)

    def _on_enter(self, idx: int) -> None:
        if self._active_drag is None or self._drop_handler is None:
            return
        self._drop_target_idx = idx
        self._repaint()

    def _on_leave(self, idx: int) -> None:
        if self._drop_target_idx == idx:
            self._drop_target_idx = None
            self._repaint()

    def _clear_drop_targets(self) -> None:
        self._drop_target_idx = None
        self._repaint()

    def _repaint(self) -> None:
        for i, row in enumerate(self._rows):
            selected = i in self._selected
            if self._drop_target_idx == i:
                row.configure(fg_color=self._DROP)
            elif self._tints.get(i):
                row.configure(fg_color=self._tints[i])
            else:
                row.configure(fg_color=self._ROW)
            # Text color: blue when selected, normal otherwise
            text_color = self._SEL_TEXT if selected else self._DIM_TEXT
            for lbl in self._labels.get(i, []):
                try:
                    lbl.configure(text_color=text_color)
                except Exception:
                    pass


class _CtkTable:
    """CTK scrollable table replacing ttk.Treeview."""

    _SEL = ("#1f6aa5", "#1f538d")
    _ROW = ("gray94", "gray18")
    _HDR = ("gray85", "gray22")

    def __init__(self, parent: ctk.CTkFrame, columns: tuple[str, ...],
                 height: int = 8, font: ctk.CTkFont | None = None,
                 heading_font: ctk.CTkFont | None = None) -> None:
        self._cols = columns
        self._font = font
        self._hfont = heading_font
        self._widths: dict[str, int] = {c: 120 for c in columns}
        self._col_text: dict[str, str] = {c: c for c in columns}
        self._data: dict[str, list[str]] = {}
        self._order: list[str] = []
        self._widgets: dict[str, ctk.CTkFrame] = {}
        self._sel: str | None = None
        self._cbs: list = []
        self._outer = ctk.CTkFrame(parent, fg_color="transparent")
        self._hdr_frame = ctk.CTkFrame(self._outer, fg_color=self._HDR, corner_radius=6)
        self._hdr_frame.pack(fill="x", pady=(0, 2))
        self._body = ctk.CTkScrollableFrame(self._outer, height=height * 32, fg_color="transparent")
        self._body.pack(fill="both", expand=True)
        if hasattr(self._body, "_scrollbar"):
            self._body._scrollbar.grid_remove()
        self._hlabels: dict[str, ctk.CTkLabel] = {}
        self._rebuild_header()

    def _update_scrollbar(self) -> None:
        """Show scrollbar only when rows overflow the queue viewport."""
        sf = self._body
        if not hasattr(sf, "_scrollbar") or not hasattr(sf, "_parent_canvas"):
            return

        def _check() -> None:
            try:
                canvas = sf._parent_canvas
                canvas.update_idletasks()
                bbox = canvas.bbox("all")
                content_h = bbox[3] if bbox else 0
                view_h = canvas.winfo_height()
                if content_h > view_h:
                    sf._scrollbar.grid()
                else:
                    sf._scrollbar.grid_remove()
            except Exception:
                pass

        sf.after(50, _check)

    def _rebuild_header(self) -> None:
        for w in self._hdr_frame.winfo_children():
            w.destroy()
        self._hlabels.clear()
        for col in self._cols:
            lbl = ctk.CTkLabel(
                self._hdr_frame, text=self._col_text[col],
                font=self._hfont, anchor="w", width=self._widths[col],
            )
            lbl.pack(side="left", padx=(8, 0), pady=7)
            self._hlabels[col] = lbl

    def pack(self, **kw) -> None: self._outer.pack(**kw)
    def configure(self, **kw) -> None: self._outer.configure(**kw)

    def heading(self, col: str, text: str = "", **_) -> None:
        self._col_text[col] = text
        if col in self._hlabels:
            self._hlabels[col].configure(text=text)

    def column(self, col: str, width: int = 120, **_) -> None:
        self._widths[col] = width
        self._rebuild_header()

    def insert(self, _parent, _idx, iid: str, values: tuple = ()) -> None:
        self._data[iid] = [str(v) for v in values]
        self._order.append(iid)
        self._build_row(iid)
        self._update_scrollbar()

    def _build_row(self, iid: str) -> None:
        vals = self._data.get(iid, [])
        row = ctk.CTkFrame(self._body, fg_color=self._ROW, corner_radius=4, cursor="hand2")
        row.pack(fill="x", pady=1, padx=2)
        for i, col in enumerate(self._cols):
            ctk.CTkLabel(
                row, text=vals[i] if i < len(vals) else "",
                anchor="w", width=self._widths[col], font=self._font,
            ).pack(side="left", padx=(8, 0), pady=5)
        for w in (row, *row.winfo_children()):
            w.bind("<Button-1>", lambda e, i=iid: self._click(i))
        self._widgets[iid] = row

    def delete(self, iid: str) -> None:
        if iid in self._widgets:
            self._widgets.pop(iid).destroy()
        self._data.pop(iid, None)
        if iid in self._order:
            self._order.remove(iid)
        if self._sel == iid:
            self._sel = None
        self._update_scrollbar()

    def get_children(self) -> list[str]:
        return list(self._order)

    def selection(self) -> tuple[str, ...]:
        return (self._sel,) if self._sel else ()

    def selection_set(self, iid: str) -> None:
        self._sel = iid
        self._repaint()

    def item(self, iid: str, values: tuple | None = None, **_) -> None:
        if values is None or iid not in self._data:
            return
        self._data[iid] = [str(v) for v in values]
        if iid in self._widgets:
            labels = [w for w in self._widgets[iid].winfo_children()
                      if isinstance(w, ctk.CTkLabel)]
            for i, lbl in enumerate(labels):
                lbl.configure(text=self._data[iid][i] if i < len(self._data[iid]) else "")

    def exists(self, iid: str) -> bool:
        return iid in self._data

    def bind(self, event: str, cb) -> None:
        if event == "<<TreeviewSelect>>":
            self._cbs.append(cb)

    def _click(self, iid: str) -> None:
        self._sel = iid
        self._repaint()
        for cb in self._cbs:
            cb(None)

    def _repaint(self) -> None:
        for iid, row in self._widgets.items():
            row.configure(fg_color=self._SEL if iid == self._sel else self._ROW)


class BlenderVideoMapperApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.system_theme = _detect_system_theme()
        self.palette = _theme_palette(self.system_theme)
        self.title("Blender Render")
        self.geometry("1180x760")
        self.minsize(980, 680)

        self.profile_path = Path.home() / ".blender_video_mapper" / "profile.json"
        self.presets_dir = Path.home() / ".blender_video_mapper" / "presets"
        self.pending_save_job: str | None = None
        self.last_run_report_path: str = ""
        self.log_file_path = Path.home() / ".blender_video_mapper" / "logs" / "app.log"

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.discovery_result_queue: queue.Queue[tuple[str, object, object, str]] = queue.Queue()
        self.is_rendering = False
        self.cancel_requested = False
        self.next_job_id = 1
        self.active_run_job_ids: list[int] = []
        self._movie_output_probe_cache: dict[str, tuple[bool, str]] = {}

        self.blender_path_var = tk.StringVar()
        self.scene_path_var = tk.StringVar()
        self.target_material_var = tk.StringVar()
        self.assignment_video_var = tk.StringVar()
        self.target_camera_var = tk.StringVar()
        self.width_var = tk.StringVar(value="1920")
        self.height_var = tk.StringVar(value="1080")
        self.fps_var = tk.StringVar(value="30")
        self.frame_start_var = tk.StringVar(value="1")
        self.frame_end_var = tk.StringVar(value="250")
        self.output_path_var = tk.StringVar()
        self.engine_var = tk.StringVar(value="CYCLES")
        self.samples_var = tk.StringVar(value="64")
        self.frame_step_var = tk.StringVar(value="1")
        self.output_profile_var = tk.StringVar(value="H264 MP4")
        self.color_view_transform_var = tk.StringVar(value="Filmic")
        self.color_look_var = tk.StringVar(value="None")
        self.color_exposure_var = tk.StringVar(value="0.0")
        self.color_gamma_var = tk.StringVar(value="1.0")
        self.timeout_seconds_var = tk.StringVar(value="0")
        self.idle_timeout_seconds_var = tk.StringVar(value="0")
        self.retry_count_var = tk.StringVar(value="0")
        self.confirm_overwrite_var = tk.BooleanVar(value=True)
        self.safe_mode_var = tk.BooleanVar(value=True)

        self.material_search_var = tk.StringVar()
        self.video_search_var = tk.StringVar()

        self.status_var = tk.StringVar(value="Ready")
        self.video_count_var = tk.StringVar(value="0 video files selected")
        self.discovery_summary_var = tk.StringVar(value="No scene scan yet")
        self.progress_caption_var = tk.StringVar(value="No render in progress")
        self.run_summary_var = tk.StringVar(value="Queue ready")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.log_collapsed = False

        self.video_files: list[str] = []
        self.jobs: list[RenderJob] = []
        self.material_assignments: list[MaterialVideoAssignment] = []
        self.discovered_materials: list[str] = []
        self.discovered_cameras: list[str] = []
        self.scan_in_progress = False
        self.last_scene_scan_signature: tuple[str, int, int] | None = None
        self.split_ratio = 0.5  # Default 50/50 split
        self.split_dragging = False
        self.split_drag_start_x = 0
        self.split_lists_frame = None

        self._load_profile()
        self._build_styles()
        self._build_ui()

        if self.video_files:
            self._refresh_video_list()
            self._sync_jobs_with_videos()
        else:
            self._refresh_video_list()

        self._setup_persistence_traces()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._initialize_blender_path)
        self.after(120, self._drain_log_queue)



    def _build_styles(self) -> None:
        ctk.set_appearance_mode("dark" if self.system_theme == "dark" else "light")
        ctk.set_default_color_theme("blue")

        self.font_headline = ctk.CTkFont(family="Avenir Next", size=16, weight="bold")
        self.font_section = ctk.CTkFont(family="Avenir Next", size=11, weight="bold")
        self.font_body = ctk.CTkFont(family="Avenir Next", size=11)
        self.font_hint = ctk.CTkFont(family="Avenir Next", size=9)
        self.font_mono = ctk.CTkFont(family="Menlo", size=10)
        self.font_btn_primary = ctk.CTkFont(family="Avenir Next", size=12, weight="bold")
        self.font_btn = ctk.CTkFont(family="Avenir Next", size=11)

        # Keep minimal palette only for ttk.Treeview (no CTK equivalent)
        self.palette = _theme_palette(self.system_theme)
        p = self.palette

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Modern.Treeview",
            background=p["input_bg"],
            fieldbackground=p["input_bg"],
            foreground=p["input_fg"],
            rowheight=28,
            font=("Avenir Next", 10),
        )
        style.configure(
            "Modern.Treeview.Heading",
            font=("Avenir Next", 10, "bold"),
            background=p["tree_header_bg"],
            foreground=p["tree_header_fg"],
        )
        style.map(
            "Modern.Treeview",
            background=[("selected", p["selection_bg"])],
            foreground=[("selected", p["selection_fg"])],
        )
        style.configure(
            "TProgressbar",
            troughcolor=p["tree_header_bg"],
            background=p["selection_bg"],
            bordercolor=p["tree_header_bg"],
            lightcolor=p["selection_bg"],
            darkcolor=p["selection_bg"],
        )

    # ── CTK section card helper ──────────────────────────────────────────────

    def _section(
        self,
        parent: ctk.CTkFrame,
        title: str,
        fill: str = "x",
        expand: bool = False,
        pady: tuple = (0, 12),
    ) -> ctk.CTkFrame:
        """Return an inner CTkFrame styled as a titled section card."""
        outer = ctk.CTkFrame(parent, corner_radius=10)
        outer.pack(fill=fill, expand=expand, pady=pady)
        if title:
            ctk.CTkLabel(
                outer, text=title, font=self.font_section, anchor="w"
            ).pack(fill="x", padx=14, pady=(10, 4))
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(
            fill="both" if fill == "both" else "x",
            expand=expand,
            padx=14,
            pady=(0, 14),
        )
        return inner

    def _build_ui(self) -> None:
        self._build_menu()

        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=10, pady=10)

        self._build_header(root)

        content = ctk.CTkFrame(root, fg_color="transparent")
        content.pack(fill="both", expand=True, pady=(4, 0))
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(content, corner_radius=10)
        self.tabs.grid(row=0, column=0, sticky="nsew")

        for tab_name in ("Scene", "Render"):
            self.tabs.add(tab_name)

        scene_scroll = ctk.CTkScrollableFrame(
            self.tabs.tab("Scene"), fg_color="transparent"
        )
        scene_scroll.pack(fill="both", expand=True)
        self._build_scene_card(scene_scroll)

        render_scroll = ctk.CTkScrollableFrame(
            self.tabs.tab("Render"), fg_color="transparent"
        )
        render_scroll.pack(fill="both", expand=True)

        render_top = ctk.CTkFrame(render_scroll, fg_color="transparent")
        render_top.pack(fill="both", expand=True)
        render_top.grid_columnconfigure(0, weight=4)
        render_top.grid_columnconfigure(1, weight=6)
        render_top.grid_rowconfigure(0, weight=1)

        render_left = ctk.CTkFrame(render_top, fg_color="transparent")
        render_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        render_right = ctk.CTkFrame(render_top, fg_color="transparent")
        render_right.grid(row=0, column=1, sticky="nsew")

        self._build_render_card(render_left)
        self._build_presets_card(render_right)
        self._build_actions_card(render_right)
        self._build_queue_card(render_right)

        # ── Pinned live-logs bar (always visible, fixed height) ──────────────
        logs_host = ctk.CTkFrame(content, corner_radius=10, height=150)
        logs_host.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        logs_host.grid_propagate(False)
        logs_host.columnconfigure(0, weight=1)
        logs_host.rowconfigure(1, weight=1)
        self._build_logs_card(logs_host)

    def _create_scrollable_tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        # Legacy stub – not used in CTK build but kept to avoid AttributeError
        # if anything calls it by name.
        raise NotImplementedError("Use CTkTabview tabs directly.")



    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        profile_menu = tk.Menu(menubar, tearoff=0)
        profile_menu.add_command(label="Properties", command=self._show_properties_dialog)
        profile_menu.add_separator()
        profile_menu.add_command(label="Save Named Preset", command=self._save_named_preset)
        profile_menu.add_command(label="Load Named Preset", command=self._load_named_preset)
        profile_menu.add_separator()
        profile_menu.add_command(label="Reset To Defaults", command=self._reset_to_defaults)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Copy Diagnostics", command=self._copy_diagnostics)
        tools_menu.add_command(label="Open Last Run Report", command=self._open_last_run_report)

        menubar.add_cascade(label="Profile", menu=profile_menu)
        menubar.add_cascade(label="Tools", menu=tools_menu)
        self.config(menu=menubar)

    def _build_header(self, parent: ctk.CTkFrame) -> None:
        # Header removed – status bar space repurposed for content
        pass

    def _build_scene_card(self, parent: ctk.CTkFrame) -> None:
        card = self._section(parent, "")
        card.columnconfigure(1, weight=1)
        card.columnconfigure(2, weight=0)
        card.columnconfigure(3, weight=0)

        self._path_row(card, 0, "3D scene file", self.scene_path_var,
                       browse_type="scene_file", button_text="Browse",
                       show_filename_only=True,
                       button_column=2,
                       button_padx=(0, 8),
                       filetypes=[
                           ("3D Files", "*.blend *.fbx *.obj *.glb *.gltf *.usd *.usda *.usdc *.abc *.stl *.ply"),
                           ("All Files", "*.*"),
                       ])

        self.scan_button = ctk.CTkButton(
            card, text="Scan Scene", command=self._scan_scene,
            width=130, font=self.font_btn,
        )
        self.scan_button.grid(row=0, column=3, sticky="ew", padx=(12, 0), pady=6)

        sep = ctk.CTkFrame(card, height=1, fg_color=("gray80", "gray30"))
        sep.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 8))

        lists = ctk.CTkFrame(card, fg_color="transparent")
        lists.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=(0, 8))
        lists.grid_columnconfigure(0, weight=int(self.split_ratio * 100))
        lists.grid_columnconfigure(2, weight=int((1 - self.split_ratio) * 100))
        lists.grid_rowconfigure(0, weight=1)
        self.split_lists_frame = lists

        # Left panel: Camera + Materials
        left_panel = ctk.CTkFrame(lists, fg_color="transparent")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left_panel.grid_rowconfigure(0, weight=0)
        left_panel.grid_rowconfigure(1, weight=0)
        left_panel.grid_rowconfigure(2, weight=0)
        left_panel.grid_rowconfigure(3, weight=1)
        left_panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left_panel, text="Camera", font=self.font_body, anchor="w").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.camera_combo = ctk.CTkComboBox(
            left_panel, variable=self.target_camera_var, values=[], state="readonly",
            font=self.font_body,
        )
        self.camera_combo.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(left_panel, text="Materials", font=self.font_section, anchor="w").grid(row=2, column=0, sticky="w", pady=(0, 2))
        mat_search = ctk.CTkEntry(left_panel, textvariable=self.material_search_var,
                                  placeholder_text="Filter materials…", height=26, font=self.font_body)
        mat_search.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        self.material_search_var.trace_add("write", lambda *_: self._refresh_material_list())
        left_panel.grid_rowconfigure(4, weight=1)
        materials_host = ctk.CTkFrame(left_panel, fg_color="transparent")
        materials_host.grid(row=4, column=0, sticky="nsew")

        # Draggable separator handle
        handle = ctk.CTkFrame(lists, width=8, fg_color=("gray70", "gray40"), cursor="hand2")
        handle.grid(row=0, column=1, sticky="ns", padx=(4, 4))
        handle.bind("<Button-1>", self._on_split_handle_press)
        handle.bind("<B1-Motion>", self._on_split_handle_drag)
        handle.bind("<ButtonRelease-1>", self._on_split_handle_release)

        videos_card = ctk.CTkFrame(lists, fg_color="transparent")
        videos_card.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        videos_card.grid_rowconfigure(1, weight=1)
        videos_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(videos_card, text="Videos", font=self.font_section, anchor="w").grid(row=0, column=0, sticky="w", padx=0, pady=(0, 2))
        videos_host = ctk.CTkFrame(videos_card, fg_color="transparent")
        videos_host.grid(row=1, column=0, sticky="nsew")

        self.material_list = _CtkList(materials_host, font=self.font_body, detail_mode="text", single_select=True)
        self.material_list.pack(fill="both", expand=True)
        self.material_list.bind("<<ListboxSelect>>", self._on_material_list_selection)
        self.material_list.set_drop_handler(self._map_dropped_video_to_material)

        video_buttons = ctk.CTkFrame(videos_host, fg_color="transparent")
        video_buttons.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ctk.CTkButton(video_buttons, text="Add", command=self._add_videos, width=60, height=26, font=self.font_btn).pack(side="left")
        ctk.CTkButton(video_buttons, text="Remove", command=self._remove_selected_video, width=78, height=26, fg_color="transparent", border_width=1, font=self.font_btn).pack(side="left", padx=(4, 0))
        ctk.CTkButton(video_buttons, text="Clear", command=self._clear_videos, width=64, height=26, fg_color="transparent", border_width=1, font=self.font_btn).pack(side="left", padx=(4, 0))

        vid_search = ctk.CTkEntry(videos_host, textvariable=self.video_search_var,
                                  placeholder_text="Filter videos…", height=26, font=self.font_body)
        vid_search.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.video_search_var.trace_add("write", lambda *_: self._refresh_video_list())

        videos_host.grid_rowconfigure(2, weight=1)
        videos_host.grid_columnconfigure(0, weight=1)
        # Listbox container for grid layout
        videos_list_frame = ctk.CTkFrame(videos_host, fg_color="transparent")
        videos_list_frame.grid(row=2, column=0, sticky="nsew")
        self.video_listbox = _CtkList(videos_list_frame, font=self.font_body, detail_mode="text", single_select=True)
        self.video_listbox.pack(fill="both", expand=True)
        self.video_listbox.bind("<<ListboxSelect>>", self._on_video_list_selection)

        if HAS_TK_DND:
            self.video_listbox.drop_target_register(DND_FILES)
            self.video_listbox.dnd_bind("<<Drop>>", self._on_video_drop)

        map_actions = ctk.CTkFrame(card, fg_color="transparent")
        map_actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(2, 0))
        ctk.CTkButton(
            map_actions, text="Apply Video",
            command=self._upsert_material_assignment,
            font=self.font_btn,
            height=30,
        ).pack(side="left")
        ctk.CTkButton(
            map_actions, text="Clear Mappings",
            command=self._clear_material_assignments,
            fg_color="transparent", border_width=1, font=self.font_btn,
            height=30,
        ).pack(side="left", padx=(6, 0))
        # Removed video count display per user request

    def _build_render_card(self, parent: ctk.CTkFrame) -> None:
        card = self._section(parent, "Output")
        card.columnconfigure(0, weight=1)

        # Output path section: larger field with actions beneath it.
        out_row = ctk.CTkFrame(card, fg_color="transparent")
        out_row.pack(fill="x", pady=(0, 4))
        out_row.columnconfigure(0, weight=1)
        ctk.CTkLabel(out_row, text="Output path", font=self.font_body).grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ctk.CTkEntry(out_row, textvariable=self.output_path_var, font=self.font_body, height=32).grid(
            row=1, column=0, sticky="ew")
        out_actions = ctk.CTkFrame(out_row, fg_color="transparent")
        out_actions.grid(row=2, column=0, sticky="w", pady=(4, 0))
        ctk.CTkButton(out_actions, text="Browse", width=76, height=26, font=self.font_btn,
                      command=lambda: self._path_row_browse_save_or_dir(self.output_path_var)).pack(side="left")
        ctk.CTkButton(out_actions, text="Open", width=64, height=26, font=self.font_btn,
                      fg_color="transparent", border_width=1,
                      command=self._open_output_location).pack(side="left", padx=(6, 0))

        # Grid of settings — 2 columns per row, compact
        g = ctk.CTkFrame(card, fg_color="transparent")
        g.pack(fill="x", pady=(4, 0))
        g.columnconfigure(1, weight=1)
        g.columnconfigure(3, weight=1)

        def _cell(row: int, col_label: int, label: str, var: tk.StringVar, width: int = 70) -> None:
            ctk.CTkLabel(g, text=label, font=self.font_body).grid(
                row=row, column=col_label, sticky="w", padx=(0 if col_label == 0 else 10, 4), pady=2)
            ctk.CTkEntry(g, textvariable=var, font=self.font_body, width=width).grid(
                row=row, column=col_label + 1, sticky="ew", pady=2)

        _cell(0, 0, "Width", self.width_var)
        _cell(0, 2, "Height", self.height_var)
        _cell(1, 0, "FPS", self.fps_var, 60)
        _cell(1, 2, "Frame step", self.frame_step_var, 60)
        _cell(2, 0, "Frame start", self.frame_start_var)
        _cell(2, 2, "Frame end", self.frame_end_var)

        eng_row = ctk.CTkFrame(card, fg_color="transparent")
        eng_row.pack(fill="x", pady=(4, 0))
        eng_row.columnconfigure(1, weight=1)
        ctk.CTkLabel(eng_row, text="Engine", font=self.font_body).grid(row=0, column=0, sticky="w", padx=(0, 6))
        ctk.CTkComboBox(eng_row, variable=self.engine_var, values=["CYCLES", "BLENDER_EEVEE"],
                        state="readonly", font=self.font_body, height=28).grid(row=0, column=1, sticky="ew")

        prof_row = ctk.CTkFrame(card, fg_color="transparent")
        prof_row.pack(fill="x", pady=(4, 0))
        prof_row.columnconfigure(1, weight=1)
        ctk.CTkLabel(prof_row, text="Profile", font=self.font_body).grid(
            row=0, column=0, sticky="w", padx=(0, 6))
        ctk.CTkComboBox(prof_row, variable=self.output_profile_var,
                        values=list(OUTPUT_PROFILES.keys()),
                        state="readonly", font=self.font_body, height=28).grid(row=0, column=1, sticky="ew")

        chk_row = ctk.CTkFrame(card, fg_color="transparent")
        chk_row.pack(fill="x", pady=(6, 0))
        ctk.CTkCheckBox(chk_row, text="Confirm overwrite", variable=self.confirm_overwrite_var,
                        font=self.font_body).pack(side="left")

    def _build_actions_card(self, parent: ctk.CTkFrame) -> None:
        card = self._section(parent, "Render")

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x")

        self.render_button = ctk.CTkButton(
            actions, text="  ▶  Render Queue Now  ",
            command=self._start_render, font=self.font_btn_primary,
            height=40,
        )
        self.render_button.pack(side="left")

        self.cancel_button = ctk.CTkButton(
            actions, text="Cancel", command=self._cancel_current_job,
            fg_color="transparent", border_width=1, font=self.font_btn,
        )
        self.cancel_button.pack(side="left", padx=(10, 0))

        prog_frame = ctk.CTkFrame(card, fg_color="transparent")
        prog_frame.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(prog_frame, textvariable=self.progress_caption_var,
                     font=self.font_hint).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", pady=(5, 0))
        ctk.CTkLabel(prog_frame, textvariable=self.run_summary_var,
                     font=self.font_hint).pack(anchor="w", pady=(4, 0))

    def _build_presets_card(self, parent: ctk.CTkFrame) -> None:
        card = self._section(parent, "Presets", pady=(0, 8))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkButton(
            row,
            text="Save Preset",
            command=self._save_named_preset,
            font=self.font_btn,
            width=120,
            height=28,
        ).pack(side="left")
        ctk.CTkButton(
            row,
            text="Load Preset",
            command=self._load_named_preset,
            font=self.font_btn,
            width=120,
            height=28,
            fg_color="transparent",
            border_width=1,
        ).pack(side="left", padx=(6, 0))

        ctk.CTkButton(
            row,
            text="Open Presets Folder",
            command=self._open_presets_folder,
            font=self.font_hint,
            fg_color="transparent",
            border_width=1,
            width=150,
            height=28,
        ).pack(side="right")

    def _path_row_browse_save_or_dir(self, var: tk.StringVar) -> None:
        selected = filedialog.asksaveasfilename(
            title="Select output MP4 (or cancel and choose a directory)",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")],
        )
        if selected:
            var.set(selected)
            return

        selected_dir = filedialog.askdirectory(title="Select output directory for batch renders")
        if selected_dir:
            var.set(selected_dir)

    def _open_output_location(self) -> None:
        output = self.output_path_var.get().strip()
        if not output:
            messagebox.showinfo("Output Path", "Choose an output path first.")
            return

        out_path = Path(output).expanduser()
        target = out_path if out_path.is_dir() else out_path.parent
        if not target.exists():
            messagebox.showinfo("Output Path", "Output folder does not exist yet.")
            return

        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            elif os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:
            messagebox.showerror("Open Output Failed", str(exc))

    def _build_queue_card(self, parent: ctk.CTkFrame) -> None:
        card = self._section(parent, "Job Queue", fill="both", expand=True, pady=(8, 0))

        self.jobs_tree = _CtkTable(
            card,
            columns=("video", "status", "progress", "attempts", "output"),
            height=8,
            font=self.font_body,
            heading_font=self.font_hint,
        )
        self.jobs_tree.heading("video", text="Job")
        self.jobs_tree.heading("status", text="Status")
        self.jobs_tree.heading("progress", text="Progress")
        self.jobs_tree.heading("attempts", text="Attempts")
        self.jobs_tree.heading("output", text="Output")
        self.jobs_tree.column("video", width=220)
        self.jobs_tree.column("status", width=100)
        self.jobs_tree.column("progress", width=90)
        self.jobs_tree.column("attempts", width=80)
        self.jobs_tree.column("output", width=360)
        self.jobs_tree.pack(fill="both", expand=True)

    def _build_logs_card(self, parent: ctk.CTkFrame) -> None:
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(8, 0))
        
        self.log_collapse_btn = ctk.CTkButton(
            hdr, text="▼", command=self._toggle_log_collapse,
            fg_color="transparent", width=24, font=self.font_hint
        )
        self.log_collapse_btn.pack(side="left", padx=(0, 6))
        
        ctk.CTkLabel(hdr, text="Live Logs", font=self.font_section).pack(side="left")
        ctk.CTkButton(hdr, text="Export", command=self._export_selected_log,
                      fg_color="transparent", border_width=1, font=self.font_hint, width=70).pack(side="right", padx=(6, 0))
        ctk.CTkButton(hdr, text="Copy Diag", command=self._copy_diagnostics,
                      fg_color="transparent", border_width=1, font=self.font_hint, width=85).pack(side="right", padx=(6, 0))
        ctk.CTkButton(hdr, text="Clear", command=self._clear_live_logs,
                      fg_color="transparent", border_width=1, font=self.font_hint, width=60).pack(side="right")

        self.log_container = ctk.CTkFrame(parent, fg_color="transparent")
        self.log_container.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.log_text = ctk.CTkTextbox(
            self.log_container,
            font=self.font_mono,
            wrap="word",
            state="disabled",
            height=5 * 18,  # ~5 lines
        )
        self.log_text.pack(fill="x", expand=False)

    def _path_row(
        self,
        parent: ctk.CTkFrame,
        row: int,
        label: str,
        var: tk.StringVar,
        browse_type: str | None = None,
        browse_title: str = "Select file",
        button_text: str = "Browse",
        filetypes: list[tuple[str, str]] | None = None,
        show_filename_only: bool = False,
        button_column: int = 3,
        button_padx: tuple = (0, 0),
    ) -> None:
        ctk.CTkLabel(parent, text=label, font=self.font_body, anchor="w").grid(
            row=row, column=0, sticky="w", pady=6, padx=(0, 8))
        
        if show_filename_only:
            # Display label showing only filename
            display_label = ctk.CTkLabel(
                parent, text=Path(var.get()).name if var.get() else "(none)", 
                font=self.font_body, anchor="w", text_color=("gray20", "gray80")
            )
            display_label.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=6)
            
            # Update label whenever variable changes
            def update_display(*_):
                display_text = Path(var.get()).name if var.get() else "(none)"
                display_label.configure(text=display_text)
            var.trace_add("write", update_display)
        else:
            ctk.CTkEntry(parent, textvariable=var, font=self.font_body).grid(
                row=row, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=6)

        if browse_type is None:
            return

        def browse() -> None:
            if browse_type == "blender":
                selected = self._browse_for_blender_executable()
                if selected:
                    var.set(selected)
                return
            if browse_type == "scene_file":
                selected = filedialog.askopenfilename(title=browse_title, filetypes=filetypes)
                if selected:
                    self._handle_scene_file_selected(selected)
                return
            if browse_type == "file":
                selected = filedialog.askopenfilename(title=browse_title, filetypes=filetypes)
                if selected:
                    var.set(selected)
                return
            if browse_type == "save_or_dir":
                self._path_row_browse_save_or_dir(var)

        ctk.CTkButton(parent, text=button_text, command=browse, width=130,
                      font=self.font_btn).grid(row=row, column=button_column, sticky="ew", padx=button_padx, pady=6)

    def _browse_for_blender_executable(self) -> str | None:
        if sys.platform == "darwin":
            selected = filedialog.askdirectory(title="Select Blender.app", initialdir="/Applications", mustexist=True)
        else:
            selected = filedialog.askopenfilename(title="Select Blender executable")

        if not selected:
            return None

        resolved = _normalize_blender_executable(selected)
        if resolved:
            return resolved

        messagebox.showerror(
            "Invalid Blender Selection",
            "The selected location does not contain a usable Blender executable.",
        )
        return None

    def _ensure_blender_executable(self, interactive: bool, reason: str = "") -> str | None:
        current_value = self.blender_path_var.get().strip()
        resolved = _find_blender_executable(current_value)
        if resolved:
            if current_value != resolved:
                self.blender_path_var.set(resolved)
            return resolved

        if current_value:
            self.blender_path_var.set("")

        if not interactive:
            return None

        reason_suffix = f" {reason}" if reason else ""
        messagebox.showinfo(
            "Locate Blender",
            f"Blender was not found automatically. Please point to Blender.app or the Blender executable{reason_suffix}.",
        )
        selected = self._browse_for_blender_executable()
        if not selected:
            return None

        self.blender_path_var.set(selected)
        self.log_queue.put(f"[app] Blender selected manually: {selected}")
        return selected

    def _initialize_blender_path(self) -> None:
        resolved = self._ensure_blender_executable(interactive=False)
        if resolved:
            self.log_queue.put(f"[app] Blender detected automatically: {resolved}")
            return

        self.log_queue.put("[app] Blender was not found automatically. Prompting for a manual location.")
        self._ensure_blender_executable(interactive=True, reason="to scan scenes and render jobs")

    def _handle_scene_file_selected(self, scene_path: str) -> None:
        normalized_scene = str(Path(scene_path).expanduser())
        previous_scene = self.scene_path_var.get().strip()
        self.scene_path_var.set(normalized_scene)

        if normalized_scene != previous_scene:
            self.last_scene_scan_signature = None
            self.discovered_materials = []
            self.discovered_cameras = []
            self.material_assignments = []
            self.discovery_summary_var.set("No scene scan yet")
            self.target_material_var.set("")
            self.assignment_video_var.set("")
            self.target_camera_var.set("")
            if hasattr(self, "material_list"):
                self.material_list.delete(0, tk.END)
            if hasattr(self, "camera_combo"):
                self.camera_combo.configure(values=[])
                self.camera_combo.set("")
            self._refresh_material_assignments_tree()
            self._sync_jobs_with_videos()

        self.log_queue.put(f"[app] Imported 3D scene: {normalized_scene}")
        self._scan_scene()

    def _setup_persistence_traces(self) -> None:
        watched = [
            self.blender_path_var,
            self.scene_path_var,
            self.target_material_var,
            self.target_camera_var,
            self.width_var,
            self.height_var,
            self.fps_var,
            self.frame_start_var,
            self.frame_end_var,
            self.output_path_var,
            self.engine_var,
            self.samples_var,
            self.frame_step_var,
            self.output_profile_var,
            self.color_view_transform_var,
            self.color_look_var,
            self.color_exposure_var,
            self.color_gamma_var,
            self.timeout_seconds_var,
            self.idle_timeout_seconds_var,
            self.retry_count_var,
            self.confirm_overwrite_var,
            self.safe_mode_var,
        ]
        for var in watched:
            var.trace_add("write", lambda *_: self._schedule_profile_save())

        self.output_path_var.trace_add("write", lambda *_: self._on_output_path_changed())

    def _on_output_path_changed(self) -> None:
        self._refresh_job_output_previews()
        self._refresh_jobs_tree()
        self._update_summary()

    def _profile_dict(self) -> dict:
        existing_videos = [p for p in self.video_files if file_exists(p)]
        valid_assignments = [
            {
                "material_name": assignment.material_name,
                "video_path": assignment.video_path,
                "mapping_mode": assignment.mapping_mode,
            }
            for assignment in self.material_assignments
            if file_exists(assignment.video_path)
        ]
        return {
            "blender_path": self.blender_path_var.get().strip(),
            "scene_path": self.scene_path_var.get().strip(),
            "target_material": self.target_material_var.get().strip(),
            "target_camera": self.target_camera_var.get().strip(),
            "width": self.width_var.get().strip(),
            "height": self.height_var.get().strip(),
            "fps": self.fps_var.get().strip(),
            "frame_start": self.frame_start_var.get().strip(),
            "frame_end": self.frame_end_var.get().strip(),
            "output_path": self.output_path_var.get().strip(),
            "engine": self.engine_var.get().strip(),
            "samples": self.samples_var.get().strip(),
            "frame_step": self.frame_step_var.get().strip(),
            "output_profile": self.output_profile_var.get().strip(),
            "color_view_transform": self.color_view_transform_var.get().strip(),
            "color_look": self.color_look_var.get().strip(),
            "color_exposure": self.color_exposure_var.get().strip(),
            "color_gamma": self.color_gamma_var.get().strip(),
            "timeout_seconds": self.timeout_seconds_var.get().strip(),
            "idle_timeout_seconds": self.idle_timeout_seconds_var.get().strip(),
            "retry_count": self.retry_count_var.get().strip(),
            "confirm_overwrite": bool(self.confirm_overwrite_var.get()),
            "safe_mode": bool(self.safe_mode_var.get()),
            "video_files": existing_videos,
            "material_assignments": valid_assignments,
        }

    def _default_settings_dict(self) -> dict:
        return {
            "blender_path": "",
            "scene_path": "",
            "target_material": "",
            "target_camera": "",
            "width": "1920",
            "height": "1080",
            "fps": "30",
            "frame_start": "1",
            "frame_end": "250",
            "output_path": "",
            "engine": "CYCLES",
            "samples": "64",
            "frame_step": "1",
            "output_profile": "H264 MP4",
            "color_view_transform": "Filmic",
            "color_look": "None",
            "color_exposure": "0.0",
            "color_gamma": "1.0",
            "timeout_seconds": "0",
            "idle_timeout_seconds": "0",
            "retry_count": "0",
            "confirm_overwrite": True,
            "safe_mode": True,
            "video_files": [],
            "material_assignments": [],
        }

    def _apply_settings(self, data: dict, include_videos: bool) -> None:
        self.blender_path_var.set(data.get("blender_path", self.blender_path_var.get()))
        self.scene_path_var.set(data.get("scene_path", self.scene_path_var.get()))
        self.target_material_var.set(data.get("target_material", self.target_material_var.get()))
        self.target_camera_var.set(data.get("target_camera", self.target_camera_var.get()))
        self.width_var.set(str(data.get("width", self.width_var.get())))
        self.height_var.set(str(data.get("height", self.height_var.get())))
        self.fps_var.set("30")  # Always default to 30; auto-updated from video
        self.frame_start_var.set(str(data.get("frame_start", self.frame_start_var.get())))
        self.frame_end_var.set(str(data.get("frame_end", self.frame_end_var.get())))
        self.output_path_var.set(data.get("output_path", self.output_path_var.get()))
        self.engine_var.set(data.get("engine", self.engine_var.get()))
        self.samples_var.set(str(data.get("samples", self.samples_var.get())))
        self.frame_step_var.set(str(data.get("frame_step", self.frame_step_var.get())))
        self.output_profile_var.set(data.get("output_profile", self.output_profile_var.get()))
        self.color_view_transform_var.set(data.get("color_view_transform", self.color_view_transform_var.get()))
        self.color_look_var.set(data.get("color_look", self.color_look_var.get()))
        self.color_exposure_var.set(str(data.get("color_exposure", self.color_exposure_var.get())))
        self.color_gamma_var.set(str(data.get("color_gamma", self.color_gamma_var.get())))
        self.timeout_seconds_var.set(str(data.get("timeout_seconds", self.timeout_seconds_var.get())))
        self.idle_timeout_seconds_var.set(str(data.get("idle_timeout_seconds", self.idle_timeout_seconds_var.get())))
        self.retry_count_var.set(str(data.get("retry_count", self.retry_count_var.get())))
        self.confirm_overwrite_var.set(bool(data.get("confirm_overwrite", self.confirm_overwrite_var.get())))
        self.safe_mode_var.set(bool(data.get("safe_mode", self.safe_mode_var.get())))

        loaded_assignments = self._normalize_material_assignments(data.get("material_assignments", []))

        if include_videos:
            loaded_videos = data.get("video_files", [])
            if isinstance(loaded_videos, list):
                self.video_files = [p for p in loaded_videos if isinstance(p, str) and file_exists(p)]

        for assignment in loaded_assignments:
            if assignment.video_path not in self.video_files:
                self.video_files.append(assignment.video_path)

        self.material_assignments = loaded_assignments

        if hasattr(self, "video_listbox"):
            self._refresh_video_list()
        if hasattr(self, "assignment_tree"):
            self._refresh_material_assignments_tree()
        if hasattr(self, "jobs_tree"):
            self._sync_jobs_with_videos()

    def _load_profile(self) -> None:
        if not self.profile_path.exists():
            return

        try:
            data = json.loads(self.profile_path.read_text())
        except Exception:
            return

        self._apply_settings(data, include_videos=True)

    def _schedule_profile_save(self) -> None:
        if self.pending_save_job:
            self.after_cancel(self.pending_save_job)
        self.pending_save_job = self.after(400, self._save_profile)

    def _save_profile(self) -> None:
        self.pending_save_job = None
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            self.profile_path.write_text(json.dumps(self._profile_dict(), indent=2))
        except Exception:
            pass

    def _show_properties_dialog(self) -> None:
        """Show properties dialog with Blender app picker."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Properties")
        dialog.geometry("400x120")
        dialog.resizable(False, False)
        dialog.grab_set()
        
        # Blender section
        blender_frame = ctk.CTkFrame(dialog, corner_radius=10)
        blender_frame.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(blender_frame, text="Blender", font=self.font_section, anchor="w").pack(fill="x", padx=12, pady=(8, 4))
        inner_frame = ctk.CTkFrame(blender_frame, fg_color="transparent")
        inner_frame.pack(fill="both", padx=12, pady=(0, 12))
        
        blender_path_display = ctk.CTkEntry(inner_frame, textvariable=self.blender_path_var, state="readonly", font=self.font_body)
        blender_path_display.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(
            inner_frame, text="Locate Blender", command=self._browse_blender,
            font=self.font_btn, width=120
        ).pack(anchor="w")
        
        dialog.transient(self)
        dialog.after(100, dialog.lift)

    def _browse_blender(self) -> None:
        """Browse for Blender executable."""
        resolved = self._browse_for_blender_executable()
        if resolved:
            self.blender_path_var.set(resolved)
            self._schedule_profile_save()

    def _save_named_preset(self) -> None:
        name = simpledialog.askstring("Save Named Preset", "Preset name:", parent=self)
        if not name:
            return

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
        if not safe_name:
            messagebox.showerror("Invalid Name", "Preset name is invalid.")
            return

        preset_path = self.presets_dir / f"{safe_name}.json"
        try:
            self.presets_dir.mkdir(parents=True, exist_ok=True)
            preset_path.write_text(json.dumps(self._profile_dict(), indent=2))
            messagebox.showinfo("Preset Saved", f"Saved preset:\n{preset_path}")
        except Exception as exc:
            messagebox.showerror("Save Failed", str(exc))

    def _open_presets_folder(self) -> None:
        try:
            self.presets_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(self.presets_dir)])
            elif os.name == "nt":
                os.startfile(str(self.presets_dir))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(self.presets_dir)])
        except Exception as exc:
            messagebox.showerror("Open Presets Folder Failed", str(exc))

    def _load_named_preset(self) -> None:
        try:
            self.presets_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        preset_path = filedialog.askopenfilename(
            title="Load Named Preset",
            initialdir=str(self.presets_dir),
            filetypes=[("JSON", "*.json"), ("All Files", "*.*")],
        )
        if not preset_path:
            return

        try:
            data = json.loads(Path(preset_path).read_text())
            self._apply_settings(data, include_videos=True)
            self._sync_jobs_with_videos()
            self._schedule_profile_save()
            messagebox.showinfo("Preset Loaded", f"Loaded preset:\n{preset_path}")
        except Exception as exc:
            messagebox.showerror("Load Failed", str(exc))

    def _reset_to_defaults(self) -> None:
        if self.is_rendering:
            messagebox.showinfo("Busy", "Cannot reset while rendering is in progress.")
            return

        self._apply_settings(self._default_settings_dict(), include_videos=True)
        self._sync_jobs_with_videos()
        self._schedule_profile_save()

    def _on_close(self) -> None:
        self._save_profile()
        self.destroy()

    def _clone_material_assignments(
        self,
        assignments: list[MaterialVideoAssignment] | None = None,
    ) -> list[MaterialVideoAssignment]:
        source = assignments if assignments is not None else self.material_assignments
        return [
            MaterialVideoAssignment(
                material_name=assignment.material_name,
                video_path=assignment.video_path,
                mapping_mode=assignment.mapping_mode,
            )
            for assignment in source
        ]

    def _job_display_name(self, job: RenderJob) -> str:
        if job.label.strip():
            return job.label
        if job.video_path:
            return Path(job.video_path).name
        return f"Job {job.id}"

    def _current_output_profile_name(self) -> str:
        profile_name = self.output_profile_var.get().strip()
        if profile_name not in OUTPUT_PROFILES:
            return "H264 MP4"
        return profile_name

    def _current_render_options(self, profile_name: str | None = None) -> RenderOptions:
        active_profile = profile_name or self._current_output_profile_name()
        output_format, codec = OUTPUT_PROFILES.get(active_profile, ("MPEG4", "H264"))
        return RenderOptions(
            width=int(self.width_var.get()),
            height=int(self.height_var.get()),
            fps=int(self.fps_var.get()),
            frame_start=int(self.frame_start_var.get()),
            frame_end=int(self.frame_end_var.get()),
            engine=self.engine_var.get().strip().upper(),
            samples=int(self.samples_var.get()),
            frame_step=int(self.frame_step_var.get()),
            output_format=output_format,
            codec=codec,
            color_view_transform=self.color_view_transform_var.get().strip() or "Filmic",
            color_look=self.color_look_var.get().strip() or "None",
            color_exposure=float(self.color_exposure_var.get()),
            color_gamma=float(self.color_gamma_var.get()),
            timeout_seconds=int(self.timeout_seconds_var.get()),
            idle_timeout_seconds=int(self.idle_timeout_seconds_var.get()),
        )

    def _capture_job_snapshot(
        self,
        job: RenderJob,
        assignments: list[MaterialVideoAssignment],
        *,
        force: bool = False,
    ) -> None:
        """Freeze current Scene/Render settings into the job.
        Existing snapshots are preserved unless force=True."""
        if not force and job.render_options is not None:
            return

        job.material_assignments = self._clone_material_assignments(assignments)
        job.scene_path = self.scene_path_var.get().strip()
        job.target_camera = self.target_camera_var.get().strip()
        job.output_input = self.output_path_var.get().strip()
        job.output_profile = self._current_output_profile_name()
        job.render_options = self._current_render_options(job.output_profile)
        job.retry_count = int(self.retry_count_var.get())
        job.safe_mode = bool(self.safe_mode_var.get())

        if not job.name_seed:
            if assignments:
                if len(assignments) == 1:
                    a = assignments[0]
                    job.name_seed = f"{a.material_name}_{Path(a.video_path).stem}"
                else:
                    job.name_seed = f"mapped_scene_{len(assignments)}"
            elif job.video_path:
                job.name_seed = Path(job.video_path).stem
            else:
                job.name_seed = f"job_{job.id}"

    def _refresh_job_output_previews(self) -> None:
        """Populate queue output previews and use output filename for queue job names."""
        if not self.jobs:
            return

        batch_mode = len(self.jobs) > 1

        for job in self.jobs:
            source_video = self._job_source_video(job)
            if not source_video:
                continue

            output_input = job.output_input or self.output_path_var.get().strip()
            scene_path = job.scene_path or self.scene_path_var.get().strip()
            base_label = job.name_seed or f"job_{job.id}"

            try:
                preview = resolve_output_path(
                    output_input=output_input,
                    scene_path=scene_path,
                    video_path=source_video,
                    is_batch=batch_mode,
                    job_label=base_label,
                )
            except Exception:
                preview = ""

            job.output_path = preview
            if preview:
                job.label = Path(preview).name

    def _composite_job_label(self, assignments: list[MaterialVideoAssignment] | None = None) -> str:
        active = assignments if assignments is not None else self.material_assignments
        if not active:
            return "Mapped scene render"
        if len(active) == 1:
            assignment = active[0]
            return f"{assignment.material_name} <- {Path(assignment.video_path).stem}"
        return f"Mapped scene ({len(active)} materials)"

    def _job_material_assignments(self, job: RenderJob) -> list[MaterialVideoAssignment]:
        if job.material_assignments:
            return self._clone_material_assignments(job.material_assignments)

        material_name = self.target_material_var.get().strip()
        if not material_name or not job.video_path:
            return []

        return [
            MaterialVideoAssignment(
                material_name=material_name,
                video_path=job.video_path,
                mapping_mode=VIDEO_MAPPING_MODE_EMISSION,
            )
        ]

    def _job_source_video(self, job: RenderJob) -> str:
        assignments = self._job_material_assignments(job)
        if assignments:
            return assignments[0].video_path
        return job.video_path

    def _scene_signature(self, scene_path: str) -> tuple[str, int, int] | None:
        try:
            resolved = Path(scene_path).expanduser().resolve()
            stat = resolved.stat()
            return (str(resolved), stat.st_mtime_ns, stat.st_size)
        except Exception:
            return None

    def _cached_scene_discovery(self, scene_path: str) -> tuple[list[str], list[str]] | None:
        signature = self._scene_signature(scene_path)
        if signature is None or signature != self.last_scene_scan_signature:
            return None
        return list(self.discovered_materials), list(self.discovered_cameras)

    def _cache_scene_discovery(self, scene_path: str, materials: list[str], cameras: list[str]) -> None:
        self.discovered_materials = list(materials)
        self.discovered_cameras = list(cameras)
        self.last_scene_scan_signature = self._scene_signature(scene_path)

    def _discovery_timeout_seconds(self) -> int:
        try:
            configured = int(self.timeout_seconds_var.get())
        except ValueError:
            configured = 0
        return configured if configured > 0 else DISCOVERY_DEFAULT_TIMEOUT_SECONDS

    def _refresh_assignment_video_choices(self) -> None:
        values = [path for path in self.video_files if file_exists(path)]
        current = self.assignment_video_var.get().strip()
        if current in values:
            return

        self.assignment_video_var.set(values[0] if values else "")

    def _refresh_material_assignments_tree(self) -> None:
        if not hasattr(self, "assignment_tree"):
            return

        previous_selection = self.assignment_tree.selection()
        selected_material = None
        if previous_selection:
            try:
                selected_material = self.material_assignments[int(previous_selection[0])].material_name
            except (IndexError, ValueError):
                selected_material = None

        for item in self.assignment_tree.get_children():
            self.assignment_tree.delete(item)

        for idx, assignment in enumerate(self.material_assignments):
            self.assignment_tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    assignment.material_name,
                    Path(assignment.video_path).name,
                ),
            )

        if selected_material:
            for idx, assignment in enumerate(self.material_assignments):
                if assignment.material_name == selected_material:
                    self.assignment_tree.selection_set(str(idx))
                    break

    # Palette of distinct colors for material-video pair indicators
    _INDICATOR_PALETTE = [
        "#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#1abc9c",
        "#3498db", "#9b59b6", "#e91e63", "#00bcd4", "#8bc34a",
        "#ff5722", "#607d8b", "#ff9800", "#4caf50", "#2196f3",
    ]

    def _assignment_color(self, material_name: str) -> str | None:
        """Return a consistent color for a given material assignment, or None if unassigned."""
        assigned = [a.material_name for a in self.material_assignments]
        if material_name not in assigned:
            return None
        idx = assigned.index(material_name)
        return self._INDICATOR_PALETTE[idx % len(self._INDICATOR_PALETTE)]

    def _refresh_assignment_indicators(self) -> None:
        """Update colored circle indicators for assigned materials and videos."""
        if not hasattr(self, "material_list") or not hasattr(self, "video_listbox"):
            return

        # Build material->color and video->color maps
        mat_color: dict[str, str] = {}
        vid_color: dict[str, str] = {}
        for i, a in enumerate(self.material_assignments):
            color = self._INDICATOR_PALETTE[i % len(self._INDICATOR_PALETTE)]
            mat_color[a.material_name] = color
            vid_color[a.video_path] = color

        # Update material indicators
        for idx, material_path in enumerate(self.material_list._items):
            material_name = Path(material_path).name if '/' in material_path or '\\' in material_path else material_path
            self.material_list.set_indicator(idx, mat_color.get(material_name))

        # Update video indicators
        for idx, video_path in enumerate(self.video_listbox._items):
            self.video_listbox.set_indicator(idx, vid_color.get(video_path))

    def _normalize_material_assignments(self, raw_assignments: object) -> list[MaterialVideoAssignment]:
        normalized: list[MaterialVideoAssignment] = []
        if not isinstance(raw_assignments, list):
            return normalized

        for item in raw_assignments:
            if not isinstance(item, dict):
                continue

            material_name = str(item.get("material_name") or item.get("material") or "").strip()
            video_path = str(item.get("video_path") or item.get("video") or "").strip()
            mapping_mode = str(item.get("mapping_mode", VIDEO_MAPPING_MODE_EMISSION)).strip().upper()

            if not material_name or not video_path or not file_exists(video_path):
                continue
            if mapping_mode not in VIDEO_MAPPING_MODE_LABELS:
                mapping_mode = VIDEO_MAPPING_MODE_EMISSION

            normalized.append(
                MaterialVideoAssignment(
                    material_name=material_name,
                    video_path=video_path,
                    mapping_mode=mapping_mode,
                )
            )

        return normalized

    def _prune_invalid_material_assignments(self) -> None:
        allowed_videos = set(self.video_files)
        self.material_assignments = [
            assignment
            for assignment in self.material_assignments
            if assignment.video_path in allowed_videos and file_exists(assignment.video_path)
        ]

    def _on_video_list_selection(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "video_listbox"):
            return

        selected_indices = list(self.video_listbox.curselection())
        if len(selected_indices) != 1:
            return

        idx = selected_indices[0]
        if 0 <= idx < len(self.video_files):
            self.assignment_video_var.set(self.video_files[idx])

    def _on_split_handle_press(self, event: tk.Event) -> None:
        """Start dragging the split handle."""
        self.split_dragging = True
        self.split_drag_start_x = event.x_root

    def _on_split_handle_drag(self, event: tk.Event) -> None:
        """Drag the split handle to resize panels."""
        if not self.split_dragging or self.split_lists_frame is None:
            return
        
        frame_width = self.split_lists_frame.winfo_width()
        if frame_width <= 1:
            return
        
        # Calculate delta from start position
        delta_x = event.x_root - self.split_drag_start_x
        self.split_drag_start_x = event.x_root
        
        # Update split ratio (ensure it stays between 0.2 and 0.8)
        self.split_ratio = max(0.2, min(0.8, self.split_ratio + delta_x / frame_width))
        
        # Update column weights
        left_weight = int(self.split_ratio * 100)
        right_weight = int((1 - self.split_ratio) * 100)
        self.split_lists_frame.grid_columnconfigure(0, weight=left_weight)
        self.split_lists_frame.grid_columnconfigure(2, weight=right_weight)

    def _on_split_handle_release(self, event: tk.Event) -> None:
        """Stop dragging the split handle."""
        self.split_dragging = False

    def _on_material_list_selection(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "material_list"):
            return
        material_name = self.material_list.selected_value().strip()
        if material_name:
            self.target_material_var.set(material_name)

    def _map_dropped_video_to_material(self, video_path: str, material_index: int) -> None:
        if 0 <= material_index < len(self.discovered_materials):
            self.target_material_var.set(self.discovered_materials[material_index])
            if hasattr(self, "material_list"):
                self.material_list.selection_set(material_index)
            self.assignment_video_var.set(video_path)
            self._upsert_material_assignment()

    def _on_assignment_tree_selected(self, _event: tk.Event | None = None) -> None:
        selection = self.assignment_tree.selection() if hasattr(self, "assignment_tree") else ()
        if not selection:
            return

        try:
            assignment = self.material_assignments[int(selection[0])]
        except (IndexError, ValueError):
            return

        self.target_material_var.set(assignment.material_name)
        self.assignment_video_var.set(assignment.video_path)
        if hasattr(self, "material_list"):
            try:
                material_idx = self.discovered_materials.index(assignment.material_name)
                self.material_list.selection_set(material_idx)
            except ValueError:
                pass

    def _upsert_material_assignment(self) -> None:
        material_name = self.target_material_var.get().strip()
        video_path = self.assignment_video_var.get().strip()
        if not video_path:
            selected_indices = list(self.video_listbox.curselection()) if hasattr(self, "video_listbox") else []
            if len(selected_indices) == 1:
                idx = selected_indices[0]
                if 0 <= idx < len(self.video_files):
                    video_path = self.video_files[idx]
            elif self.video_files:
                video_path = self.video_files[0]
            self.assignment_video_var.set(video_path)
        if not material_name:
            messagebox.showerror("Mapping Error", "Select a material before adding a mapping.")
            return
        if not video_path or not file_exists(video_path):
            messagebox.showerror("Mapping Error", "Select a valid video before adding a mapping.")
            return

        if video_path not in self.video_files:
            self.video_files.append(video_path)
            self._refresh_video_list()

        replacement = MaterialVideoAssignment(
            material_name=material_name,
            video_path=video_path,
            mapping_mode=VIDEO_MAPPING_MODE_EMISSION,
        )

        selected_index = None
        for idx, existing in enumerate(self.material_assignments):
            if existing.material_name == material_name:
                self.material_assignments[idx] = replacement
                selected_index = idx
                self.log_queue.put(
                    f"[app] Updated material mapping: {material_name} <- {Path(video_path).name}"
                )
                break

        if selected_index is None:
            self.material_assignments.append(replacement)
            selected_index = len(self.material_assignments) - 1
            self.log_queue.put(
                f"[app] Added material mapping: {material_name} <- {Path(video_path).name}"
            )

        self._refresh_material_assignments_tree()
        self._refresh_assignment_indicators()
        if hasattr(self, "assignment_tree"):
            self.assignment_tree.selection_set(str(selected_index))
        self._sync_jobs_with_videos()
        self._schedule_profile_save()

    def _remove_selected_material_assignment(self) -> None:
        if not hasattr(self, "assignment_tree"):
            return

        selection = list(self.assignment_tree.selection())
        if not selection:
            return

        removed: list[MaterialVideoAssignment] = []
        for item in sorted(selection, key=int, reverse=True):
            idx = int(item)
            if 0 <= idx < len(self.material_assignments):
                removed.append(self.material_assignments.pop(idx))

        if not removed:
            return

        self._refresh_material_assignments_tree()
        self._refresh_assignment_indicators()
        self._sync_jobs_with_videos()
        self._schedule_profile_save()
        self.log_queue.put(f"[app] Removed {len(removed)} material mapping(s).")

    def _clear_material_assignments(self) -> None:
        if not self.material_assignments:
            return

        self.material_assignments = []
        self._refresh_material_assignments_tree()
        self._refresh_assignment_indicators()
        self._sync_jobs_with_videos()
        self._schedule_profile_save()
        self.log_queue.put("[app] Cleared all material mappings.")

    def _parse_dropped_paths(self, data: str) -> list[str]:
        try:
            candidates = list(self.tk.splitlist(data))
        except Exception:
            candidates = data.split()

        parsed: list[str] = []
        for raw in candidates:
            cleaned = raw.strip().strip("{}")
            if not cleaned:
                continue
            parsed.append(cleaned)
        return parsed

    def _probe_video_info(self, path: str) -> tuple[int, int, float] | None:
        """Return (frame_count, start_frame, fps) by parsing the video container.
        Uses a pure-Python ISO Base Media File Format (MP4/MOV) box parser so no
        external tool (ffprobe/ffmpeg) is required."""
        try:
            info = self._parse_mp4_info(path)
            if info:
                nb_frames, fps = info
                return (nb_frames, 1, fps)
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_mp4_info(path: str) -> tuple[int, float] | None:
        """Parse MP4/MOV boxes to extract (frame_count, fps). No external tools needed."""
        import struct as _struct
        results: dict = {}

        def walk(f, end_pos: int, depth: int = 0) -> None:
            while f.tell() < end_pos:
                pos = f.tell()
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size, = _struct.unpack(">I", hdr[:4])
                btype = hdr[4:8].decode("latin-1")
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size, = _struct.unpack(">Q", ext)
                    hlen = 16
                elif size == 0:
                    size = end_pos - pos
                    hlen = 8
                else:
                    hlen = 8
                content_end = pos + size
                if size < hlen:
                    break
                if btype in {"moov", "trak", "mdia", "minf", "stbl"} and depth < 8:
                    walk(f, content_end, depth + 1)
                elif btype == "mdhd":
                    version = _struct.unpack("B", f.read(1))[0]
                    f.read(3)
                    if version == 1:
                        f.read(16)
                        ts, = _struct.unpack(">I", f.read(4))
                    else:
                        f.read(8)
                        ts, = _struct.unpack(">I", f.read(4))
                    if ts > 0:
                        results["mdhd_ts"] = ts
                elif btype == "stts":
                    f.read(4)
                    cnt, = _struct.unpack(">I", f.read(4))
                    entries = [_struct.unpack(">II", f.read(8)) for _ in range(min(cnt, 200_000))]
                    if "stts" not in results:
                        results["stts"] = entries
                f.seek(content_end)

        try:
            import os as _os
            with open(path, "rb") as f:
                walk(f, _os.path.getsize(path))
            if "stts" in results and "mdhd_ts" in results:
                stts = results["stts"]
                ts = results["mdhd_ts"]
                total = sum(sc for sc, _ in stts)
                if total > 0 and ts > 0:
                    avg_dur = sum(sc * sd for sc, sd in stts) / total
                    fps = round(ts / avg_dur, 3) if avg_dur > 0 else 0.0
                    return total, fps
        except Exception:
            pass
        return None

    def _add_video_paths(self, paths: list[str]) -> None:
        changed = False
        was_empty = len(self.video_files) == 0
        first_new: str | None = None
        for path in paths:
            p = Path(path).expanduser()
            if p.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            p_str = str(p)
            if file_exists(p_str) and p_str not in self.video_files:
                self.video_files.append(p_str)
                if first_new is None:
                    first_new = p_str
                changed = True

        if changed:
            # Auto-set FPS and frame range from first new video
            if first_new:
                info = self._probe_video_info(first_new)
                if info:
                    nb_frames, start, fps = info
                    fps_str = str(int(fps)) if fps == int(fps) else str(fps)
                    self.fps_var.set(fps_str)
                    self.frame_start_var.set(str(start))
                    self.frame_end_var.set(str(nb_frames))
            self._refresh_video_list()
            self._sync_jobs_with_videos()
            self._schedule_profile_save()

    def _browse_assignment_video(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select video for material mapping",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")],
        )
        if not selected:
            return

        path = str(Path(selected).expanduser())
        self._add_video_paths([path])
        self.assignment_video_var.set(path)

    def _on_video_drop(self, event: tk.Event) -> str:
        dropped = self._parse_dropped_paths(event.data)
        self._add_video_paths(dropped)
        return "break"

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        palettes = self._status_chip_palette()
        bg, fg = palettes.get(text, (self.palette["status_default_bg"], self.palette["status_default_fg"]))
        if hasattr(self, "status_chip"):
            self.status_chip.configure(fg_color=bg, text_color=fg)

    def _status_chip_palette(self) -> dict[str, tuple[str, str]]:
        if self.system_theme == "dark":
            return {
                "Ready": ("#123524", "#86efac"),
                "Scanning scene...": ("#3f2d0f", "#fcd34d"),
                "Rendering...": ("#102d4a", "#93c5fd"),
                "Retrying failed jobs...": ("#102d4a", "#93c5fd"),
                "Cancelling...": ("#4a2508", "#fdba74"),
                "Done": ("#123524", "#86efac"),
                "Finished with failures": ("#4a1212", "#fca5a5"),
                "Cancelled": ("#4a2508", "#fdba74"),
                "Failed": ("#4a1212", "#fca5a5"),
            }

        return {
            "Ready": ("#dff3e8", "#0b6b3a"),
            "Scanning scene...": ("#fff6d8", "#7a5600"),
            "Rendering...": ("#dcedff", "#0b4ea2"),
            "Retrying failed jobs...": ("#dcedff", "#0b4ea2"),
            "Cancelling...": ("#ffe7ce", "#8b3a00"),
            "Done": ("#dff3e8", "#0b6b3a"),
            "Finished with failures": ("#ffe1e1", "#9e1c1c"),
            "Cancelled": ("#ffe7ce", "#8b3a00"),
            "Failed": ("#ffe1e1", "#9e1c1c"),
        }

    def _add_videos(self) -> None:
        selections = filedialog.askopenfilenames(
            title="Select video file(s)",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")],
        )
        if not selections:
            return

        self._add_video_paths(list(selections))

    def _remove_selected_video(self) -> None:
        selected_indices = list(self.video_listbox.curselection())
        if not selected_indices:
            return

        for idx in reversed(selected_indices):
            self.video_files.pop(idx)
        self._prune_invalid_material_assignments()
        self._refresh_video_list()
        self._refresh_material_assignments_tree()
        self._sync_jobs_with_videos()
        self._schedule_profile_save()

    def _clear_videos(self) -> None:
        self.video_files = []
        self.material_assignments = []
        self._refresh_video_list()
        self._refresh_material_assignments_tree()
        self._sync_jobs_with_videos()
        self._schedule_profile_save()

    def _refresh_video_list(self) -> None:
        query = self.video_search_var.get().strip().lower()
        self.video_listbox.delete(0, tk.END)
        for path in self.video_files:
            if not query or query in Path(path).name.lower():
                self.video_listbox.insert(tk.END, path)
        count = len(self.video_files)
        suffix = "file" if count == 1 else "files"
        self.video_count_var.set(f"{count} video {suffix} selected")
        self._refresh_assignment_video_choices()

    def _sync_jobs_with_videos(self) -> None:
        if self.material_assignments:
            assignments = self._clone_material_assignments()
            existing = next((job for job in self.jobs if job.material_assignments), None)
            if existing is None or existing.material_assignments != assignments:
                existing = RenderJob(id=self.next_job_id)
                self.next_job_id += 1

            existing.video_path = assignments[0].video_path if assignments else ""
            existing.label = existing.label or self._composite_job_label(assignments)
            self._capture_job_snapshot(existing, assignments)
            self.jobs = [existing]
            self._refresh_job_output_previews()
            self._refresh_jobs_tree()
            self._update_summary()
            return

        existing = {job.video_path: job for job in self.jobs if not job.material_assignments}
        synced: list[RenderJob] = []

        for video in self.video_files:
            if video in existing:
                job = existing[video]
                default_material = self.target_material_var.get().strip()
                assignments = []
                if default_material:
                    assignments = [
                        MaterialVideoAssignment(
                            material_name=default_material,
                            video_path=video,
                            mapping_mode=VIDEO_MAPPING_MODE_EMISSION,
                        )
                    ]
                self._capture_job_snapshot(job, assignments)
                synced.append(job)
            else:
                job = RenderJob(id=self.next_job_id, video_path=video)
                default_material = self.target_material_var.get().strip()
                assignments = []
                if default_material:
                    assignments = [
                        MaterialVideoAssignment(
                            material_name=default_material,
                            video_path=video,
                            mapping_mode=VIDEO_MAPPING_MODE_EMISSION,
                        )
                    ]
                self._capture_job_snapshot(job, assignments, force=True)
                synced.append(job)
                self.next_job_id += 1

        self.jobs = synced
        self._refresh_job_output_previews()
        self._refresh_jobs_tree()
        self._update_summary()

    def _refresh_jobs_tree(self) -> None:
        for item in self.jobs_tree.get_children():
            self.jobs_tree.delete(item)

        for job in self.jobs:
            self.jobs_tree.insert(
                "",
                tk.END,
                iid=str(job.id),
                values=(
                    self._job_display_name(job),
                    job.status,
                    f"{job.progress:.0f}%",
                    job.attempts,
                    job.output_path,
                ),
            )

    def _update_job_row(self, job: RenderJob) -> None:
        if not self.jobs_tree.exists(str(job.id)):
            self._refresh_jobs_tree()
            return

        self.jobs_tree.item(
            str(job.id),
            values=(
                self._job_display_name(job),
                job.status,
                f"{job.progress:.0f}%",
                job.attempts,
                job.output_path,
            ),
        )

    def _scan_scene(self) -> None:
        blender = self._ensure_blender_executable(interactive=True, reason="to scan the selected 3D scene")
        scene = self.scene_path_var.get().strip()

        if not blender:
            messagebox.showerror("Validation Error", "Blender executable path is required.")
            return
        if not scene or not file_exists(scene):
            messagebox.showerror("Validation Error", "Valid 3D scene file is required.")
            return

        cached = self._cached_scene_discovery(scene)
        if cached is not None:
            self.log_queue.put("[app] Using cached scene scan results.")
            self._apply_discovery_results(cached[0], cached[1], scene)
            return

        if self.scan_in_progress:
            self.log_queue.put("[app] Scene scan already running; ignoring duplicate request.")
            return

        self.scan_in_progress = True
        self._set_status("Scanning scene...")
        if hasattr(self, "scan_button"):
            self.scan_button.configure(state="disabled")
        self.log_queue.put("[app] Scanning scene for materials and cameras...")

        def worker() -> None:
            try:
                materials, cameras = discover_scene_elements(
                    blender_executable=blender,
                    discovery_script_path=self._resolve_discovery_script(),
                    scene_path=scene,
                    on_log=lambda line: self.log_queue.put(line),
                    hard_timeout_seconds=self._discovery_timeout_seconds(),
                )
                self.discovery_result_queue.put(("ok", materials, cameras, scene))
            except Exception as exc:
                self.discovery_result_queue.put(("err", str(exc), None, scene))
            finally:
                self.discovery_result_queue.put(("done", None, None, scene))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_material_list(self) -> None:
        if not hasattr(self, "material_list"):
            return
        query = self.material_search_var.get().strip().lower()
        self.material_list.delete(0, tk.END)
        for material in self.discovered_materials:
            if not query or query in material.lower():
                self.material_list.insert(tk.END, material)
        self._refresh_assignment_indicators()

    def _apply_discovery_results(self, materials: list[str], cameras: list[str], scene_path: str) -> None:
        self._cache_scene_discovery(scene_path, materials, cameras)
        self.discovered_materials = list(materials)
        self.discovered_cameras = list(cameras)

        self.camera_combo.configure(values=cameras)
        self._refresh_material_list()

        self.material_assignments = [
            assignment for assignment in self.material_assignments if assignment.material_name in materials
        ]
        self._refresh_material_assignments_tree()
        self._refresh_assignment_indicators()
        self._sync_jobs_with_videos()

        material_value = self.target_material_var.get().strip()
        camera_value = self.target_camera_var.get().strip()
        if material_value not in materials:
            material_value = materials[0] if materials else ""
        if camera_value not in cameras:
            camera_value = cameras[0] if cameras else ""

        self.target_material_var.set(material_value)
        self.target_camera_var.set(camera_value)
        if hasattr(self, "material_list") and material_value in materials:
            self.material_list.selection_set(materials.index(material_value))
        self.camera_combo.set(camera_value)

        self.log_queue.put(f"[app] Discovery complete: {len(materials)} materials, {len(cameras)} cameras")
        self.discovery_summary_var.set(f"Discovered {len(materials)} materials and {len(cameras)} cameras")
        self._set_status("Ready")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self._write_log_file(text)

    def _write_log_file(self, text: str) -> None:
        try:
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_log_if_needed(self.log_file_path)
            with self.log_file_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _rotate_log_if_needed(self, path: Path, max_bytes: int = 1_000_000) -> None:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        backup = path.with_suffix(".log.1")
        backup.unlink(missing_ok=True)
        path.rename(backup)

    def _build_diagnostics_text(self) -> str:
        now = datetime.now().isoformat(timespec="seconds")
        lines = [
            f"timestamp: {now}",
            f"status: {self.status_var.get()}",
            f"blender_path: {self.blender_path_var.get()}",
            f"scene_path: {self.scene_path_var.get()}",
            f"material: {self.target_material_var.get()}",
            f"camera: {self.target_camera_var.get()}",
            f"material_assignments: {len(self.material_assignments)}",
            f"resolution: {self.width_var.get()}x{self.height_var.get()}",
            f"fps: {self.fps_var.get()}",
            f"frame_range: {self.frame_start_var.get()}-{self.frame_end_var.get()} step={self.frame_step_var.get()}",
            f"output_profile: {self.output_profile_var.get()}",
            f"engine: {self.engine_var.get()} samples={self.samples_var.get()}",
            f"timeouts: hard={self.timeout_seconds_var.get()} idle={self.idle_timeout_seconds_var.get()}",
            f"retries: {self.retry_count_var.get()}",
            f"video_count: {len(self.video_files)}",
            f"queue_summary: {self.run_summary_var.get()}",
            f"last_run_report: {self.last_run_report_path or 'n/a'}",
        ]
        return "\n".join(lines)

    def _copy_diagnostics(self) -> None:
        data = self._build_diagnostics_text()
        self.clipboard_clear()
        self.clipboard_append(data)
        messagebox.showinfo("Diagnostics Copied", "Diagnostics copied to clipboard.")

    def _open_last_run_report(self) -> None:
        if not self.last_run_report_path or not file_exists(self.last_run_report_path):
            messagebox.showinfo("No Report", "No run report is available yet.")
            return
        path = self.last_run_report_path
        try:
            if os.name == "posix":
                os.system(f'open "{path}"')
            else:
                messagebox.showinfo("Run Report", f"Latest run report:\n{path}")
        except Exception:
            messagebox.showinfo("Run Report", f"Latest run report:\n{path}")

    def _toggle_log_collapse(self) -> None:
        self.log_collapsed = not self.log_collapsed
        if self.log_collapsed:
            self.log_container.pack_forget()
            self.log_collapse_btn.configure(text="▶")
        else:
            self.log_container.pack(fill="both", expand=True, padx=8, pady=(4, 8))
            self.log_collapse_btn.configure(text="▼")

    def _clear_live_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("0.0", "end")
        self.log_text.configure(state="disabled")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(msg)

        while True:
            try:
                kind, payload_a, payload_b, scene_path = self.discovery_result_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "ok":
                materials = payload_a if isinstance(payload_a, list) else []
                cameras = payload_b if isinstance(payload_b, list) else []
                self._apply_discovery_results(materials, cameras, scene_path)
            elif kind == "err":
                error_text = str(payload_a)
                self.log_queue.put(f"[app] Discovery ERROR: {error_text}")
                self._set_status("Ready")
                messagebox.showerror("Scene Scan Failed", error_text)
            elif kind == "done":
                self.scan_in_progress = False
                if hasattr(self, "scan_button"):
                    self.scan_button.configure(state="normal")

        self.after(120, self._drain_log_queue)

    def _probe_movie_output_support(self, blender_executable: str) -> tuple[bool, str]:
        """Check whether this Blender runtime can set movie output (FFMPEG)."""
        blender_path = str(Path(blender_executable).expanduser())
        cached = self._movie_output_probe_cache.get(blender_path)
        if cached is not None:
            return cached

        expr = (
            "import bpy,json;"
            "out={'ok':True,'err':''};"
            "code=\"try:\\n s=bpy.context.scene; s.render.image_settings.file_format='FFMPEG'"
            "\\nexcept Exception as e:\\n out['ok']=False; out['err']=str(e)\";"
            "exec(code);"
            "print('BVM_FFMPEG_PROBE:'+json.dumps(out))"
        )

        try:
            proc = subprocess.run(
                [blender_path, "--background", "--factory-startup", "--python-expr", expr],
                capture_output=True,
                text=True,
                timeout=45,
            )
        except Exception as exc:
            result = (False, f"Could not probe Blender movie output support: {exc}")
            self._movie_output_probe_cache[blender_path] = result
            return result

        marker = "BVM_FFMPEG_PROBE:"
        parsed: dict[str, object] | None = None
        for line in proc.stdout.splitlines():
            if line.startswith(marker):
                try:
                    parsed = json.loads(line[len(marker) :].strip())
                except Exception:
                    parsed = None
                break

        if parsed is None:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            reason = tail[-1] if tail else "No diagnostic output from Blender probe"
            result = (False, f"Could not verify Blender movie output support: {reason}")
            self._movie_output_probe_cache[blender_path] = result
            return result

        if not bool(parsed.get("ok", False)):
            err = str(parsed.get("err", "unknown error")).strip()
            result = (
                False,
                "Selected Blender cannot produce movie output in this runtime "
                f"(FFMPEG rejected: {err}).",
            )
            self._movie_output_probe_cache[blender_path] = result
            return result

        result = (True, "")
        self._movie_output_probe_cache[blender_path] = result
        return result

    def _preflight_validate(self, include_scene_scan: bool) -> list[str]:
        errors: list[str] = []

        blender = self._ensure_blender_executable(interactive=False) or self.blender_path_var.get().strip()
        scene = self.scene_path_var.get().strip()
        output = self.output_path_var.get().strip()

        if not blender:
            errors.append("Blender executable path is required.")
        elif not Path(os.path.expanduser(blender)).exists():
            errors.append("Blender executable path does not exist.")

        if not scene or not file_exists(scene):
            errors.append("Valid 3D scene file is required.")

        if not self.video_files and not self.material_assignments:
            errors.append("At least one video file is required.")

        if not output:
            errors.append("Output file or directory is required.")
        else:
            out_path = Path(output).expanduser()
            parent = out_path if out_path.suffix == "" else out_path.parent
            if parent.exists() and not os.access(parent, os.W_OK):
                errors.append("Output location is not writable.")

        try:
            fs = int(self.frame_start_var.get())
            fe = int(self.frame_end_var.get())
            step = int(self.frame_step_var.get())
            if fe < fs:
                errors.append("Frame end must be greater than or equal to frame start.")
            if step <= 0:
                errors.append("Frame step must be greater than zero.")
        except ValueError:
            errors.append("Frame start, end, and step must be integers.")

        if include_scene_scan and not errors:
            try:
                cached = self._cached_scene_discovery(scene)
                if cached is not None:
                    materials, cameras = cached
                    self.log_queue.put("[app] Reusing cached scene scan during preflight.")
                else:
                    materials, cameras = discover_scene_elements(
                        blender_executable=blender,
                        discovery_script_path=self._resolve_discovery_script(),
                        scene_path=scene,
                        on_log=lambda line: self.log_queue.put(line),
                        hard_timeout_seconds=self._discovery_timeout_seconds(),
                    )
                    self._cache_scene_discovery(scene, materials, cameras)

                if self.material_assignments:
                    missing_materials = [
                        assignment.material_name
                        for assignment in self.material_assignments
                        if assignment.material_name not in materials
                    ]
                    if missing_materials:
                        errors.append(
                            "Mapped material(s) are not present in scene: " + ", ".join(sorted(set(missing_materials)))
                        )
                elif self.target_material_var.get().strip() not in materials:
                    errors.append("Target material is not present in scene.")
                if self.target_camera_var.get().strip() not in cameras:
                    errors.append("Target camera is not present in scene.")
            except Exception as exc:
                errors.append(f"Scene scan failed during preflight: {exc}")

        return errors

    def _run_dry_run(self) -> None:
        if not self._ensure_blender_executable(interactive=True, reason="to validate the current setup"):
            messagebox.showerror("Dry Run Failed", "Blender executable path is required.")
            return

        errors = self._preflight_validate(include_scene_scan=True)
        if errors:
            messagebox.showerror("Dry Run Failed", "\n".join(errors))
            return

        messagebox.showinfo("Dry Run Passed", "Validation checks passed. Ready to render.")

    def _start_render(self) -> None:
        if self.is_rendering:
            return

        if not self._ensure_blender_executable(interactive=True, reason="to start rendering"):
            messagebox.showerror("Validation Error", "Blender executable path is required.")
            return

        self._sync_jobs_with_videos()
        preflight_errors = self._preflight_validate(include_scene_scan=True)
        if preflight_errors:
            messagebox.showerror("Preflight Failed", "\n".join(preflight_errors))
            return

        validation_error = self._validate_inputs()
        if validation_error:
            messagebox.showerror("Validation Error", validation_error)
            return

        pending_ids = [job.id for job in self.jobs if job.status != "success"]
        if not pending_ids:
            messagebox.showinfo("Nothing To Do", "All jobs are already successful.")
            return

        if self.confirm_overwrite_var.get():
            would_overwrite = []
            self._refresh_job_output_previews()
            for job in self.jobs:
                if job.id not in pending_ids:
                    continue
                out_path = job.output_path
                if file_exists(out_path):
                    would_overwrite.append(out_path)
            if would_overwrite:
                proceed = messagebox.askyesno(
                    "Confirm Overwrite",
                    "Some output files already exist and may be overwritten. Continue?",
                )
                if not proceed:
                    return

        self.is_rendering = True
        self.cancel_requested = False
        self.active_run_job_ids = pending_ids

        self.render_button.configure(state="disabled")
        self._set_status("Rendering...")
        self.progress_caption_var.set(f"Running {len(pending_ids)} queued job(s)")
        self.progress_var.set(0)

        for job in self.jobs:
            if job.id in pending_ids and job.status != "success":
                job.progress = 0.0
                self._update_job_row(job)

        thread = threading.Thread(target=self._render_worker, args=(pending_ids,), daemon=True)
        thread.start()

    def _validate_inputs(self) -> str | None:
        blender = self._ensure_blender_executable(interactive=False)
        if not blender:
            return "Blender executable path is required."
        if not Path(blender).exists():
            return "Blender executable path does not exist."
        if not self.scene_path_var.get().strip():
            return "3D scene file is required."
        if not file_exists(self.scene_path_var.get().strip()):
            return "3D scene file does not exist."
        if not self.jobs:
            return "Add at least one video file or material mapping."

        if self.material_assignments:
            video_paths = [assignment.video_path for assignment in self.material_assignments]
        else:
            video_paths = list(self.video_files)

        for video_path in video_paths:
            if not file_exists(video_path):
                return f"Video file does not exist: {video_path}"
        if not self.material_assignments and not self.target_material_var.get().strip():
            return "Target material is required unless you add material mappings."
        if not self.target_camera_var.get().strip():
            return "Target camera is required."
        if not self.output_path_var.get().strip():
            return "Output file or directory is required."

        int_fields = [
            (self.width_var.get(), "Width"),
            (self.height_var.get(), "Height"),
            (self.fps_var.get(), "FPS"),
            (self.frame_start_var.get(), "Frame start"),
            (self.frame_end_var.get(), "Frame end"),
            (self.samples_var.get(), "Cycles samples"),
            (self.frame_step_var.get(), "Frame step"),
            (self.timeout_seconds_var.get(), "Hard timeout"),
            (self.idle_timeout_seconds_var.get(), "Idle timeout"),
            (self.retry_count_var.get(), "Retry count"),
        ]
        for value, label in int_fields:
            try:
                int(value)
            except ValueError:
                return f"{label} must be an integer."

        float_fields = [
            (self.color_exposure_var.get(), "Color exposure"),
            (self.color_gamma_var.get(), "Color gamma"),
        ]
        for value, label in float_fields:
            try:
                float(value)
            except ValueError:
                return f"{label} must be a number."

        if int(self.frame_end_var.get()) < int(self.frame_start_var.get()):
            return "Frame end must be greater than or equal to frame start."
        if int(self.frame_step_var.get()) <= 0:
            return "Frame step must be greater than zero."
        if int(self.retry_count_var.get()) < 0:
            return "Retry count must be zero or greater."
        if self.output_profile_var.get().strip() not in OUTPUT_PROFILES:
            return "Output profile is not supported."

        return None

    def _render_worker(self, job_ids: list[int]) -> None:
        worker_script = self._resolve_worker_script()
        job_map = {job.id: job for job in self.jobs}

        run_started = time.time()
        completed_durations: list[float] = []
        report_jobs: list[dict] = []

        try:
            batch_mode = len(self.jobs) > 1
            self.log_queue.put(f"[app] Starting render jobs: {len(job_ids)}")

            for idx, job_id in enumerate(job_ids, start=1):
                if self.cancel_requested:
                    break

                render_job = job_map[job_id]
                assignments = self._clone_material_assignments(render_job.material_assignments)
                if not assignments:
                    assignments = self._job_material_assignments(render_job)
                if not assignments:
                    raise RuntimeError(f"No material assignments configured for {self._job_display_name(render_job)}")

                video = assignments[0].video_path
                job_label = self._job_display_name(render_job)
                options = render_job.render_options or self._current_render_options(render_job.output_profile)
                retries = max(0, int(render_job.retry_count))
                safe_mode = bool(render_job.safe_mode)
                profile_label = render_job.output_profile or self._current_output_profile_name()
                output_format = options.output_format
                codec = options.codec
                output_path = render_job.output_path
                if not output_path:
                    output_path = resolve_output_path(
                        output_input=render_job.output_input or self.output_path_var.get().strip(),
                        scene_path=render_job.scene_path or self.scene_path_var.get().strip(),
                        video_path=video,
                        is_batch=batch_mode,
                        job_label=render_job.name_seed or job_label,
                    )
                render_job.output_path = output_path
                starting_attempts = render_job.attempts
                render_job.status = "running"
                render_job.error = ""
                render_job.progress = 0.0
                self.after(0, self._schedule_profile_save)
                self.after(0, lambda j=render_job: self._update_job_row(j))
                self.after(0, lambda i=idx, total=len(job_ids): self.progress_caption_var.set(f"Processing job {i}/{total}"))

                self.log_queue.put(f"[app] Job {idx}/{len(job_ids)}")
                self.log_queue.put(f"[app] Job: {job_label}")
                self.log_queue.put(f"[app] Output: {output_path}")
                for assignment in assignments:
                    self.log_queue.put(
                        f"[app] Mapping: {assignment.material_name} <- {Path(assignment.video_path).name}"
                    )
                attempt_started = time.time()

                def execute_profile(
                    chosen_format: str,
                    chosen_codec: str,
                    chosen_output_path: str,
                    label: str,
                ) -> tuple[int, int]:
                    job_options = RenderOptions(**options.__dict__)
                    job_options.output_format = chosen_format
                    job_options.codec = chosen_codec
                    primary_assignment = assignments[0]
                    blender_job = JobConfig(
                        scene_path=render_job.scene_path or self.scene_path_var.get().strip(),
                        video_path=primary_assignment.video_path,
                        target_material=primary_assignment.material_name,
                        target_camera=render_job.target_camera or self.target_camera_var.get().strip(),
                        output_path=chosen_output_path,
                        render=job_options,
                        safe_mode=safe_mode,
                        material_assignments=self._clone_material_assignments(assignments),
                    )

                    rc_local = 1
                    retries_used = 0
                    while retries_used <= retries:
                        rc_local = run_blender_job(
                            blender_executable=self.blender_path_var.get().strip(),
                            worker_script_path=worker_script,
                            job=blender_job,
                            on_log=lambda line, j=render_job: self._record_job_log(j, line),
                            should_cancel=lambda: self.cancel_requested,
                        )
                        if rc_local == 0 or self.cancel_requested:
                            break
                        retries_used += 1
                        if retries_used <= retries:
                            self.log_queue.put(
                                f"[app] Retrying job ({retries_used}/{retries}) using {label} for job: {job_label}"
                            )
                    return rc_local, retries_used + 1

                active_output_path = output_path
                rc, attempts_used = execute_profile(output_format, codec, active_output_path, profile_label)

                render_job.attempts = starting_attempts + attempts_used

                if self.cancel_requested:
                    render_job.status = "cancelled"
                    render_job.error = "Cancelled by user"
                    self.after(0, lambda j=render_job: self._update_job_row(j))
                    self.log_queue.put("[app] Render run cancelled by user.")
                    break

                if rc != 0:
                    render_job.status = "failed"
                    hint = self._last_worker_error_hint(render_job)
                    render_job.error = f"Blender exited with code {rc}" + (f" | {hint}" if hint else "")
                    self.after(0, lambda j=render_job: self._update_job_row(j))
                    self.log_queue.put(f"[app] Job failed: {job_label} (exit code {rc})")
                    report_jobs.append(
                        {
                            "job": job_label,
                            "video": video,
                            "assignments": [
                                {
                                    "material_name": assignment.material_name,
                                    "video_path": assignment.video_path,
                                    "mapping_mode": assignment.mapping_mode,
                                }
                                for assignment in assignments
                            ],
                            "output": active_output_path,
                            "profile": profile_label,
                            "status": "failed",
                            "attempts": attempts_used,
                            "duration_sec": round(time.time() - attempt_started, 2),
                            "error": render_job.error,
                        }
                    )
                    self.after(0, self._update_progress)
                    continue

                render_job.status = "success"
                render_job.progress = 100.0
                duration = time.time() - attempt_started
                completed_durations.append(duration)
                avg = sum(completed_durations) / max(1, len(completed_durations))
                remaining = max(0, len(job_ids) - idx)
                eta_sec = int(avg * remaining)
                self.after(0, lambda s=eta_sec: self.progress_caption_var.set(f"ETA: {s}s remaining"))

                report_jobs.append(
                    {
                        "job": job_label,
                        "video": video,
                        "assignments": [
                            {
                                "material_name": assignment.material_name,
                                "video_path": assignment.video_path,
                                "mapping_mode": assignment.mapping_mode,
                            }
                            for assignment in assignments
                        ],
                        "output": active_output_path,
                        "profile": profile_label,
                        "status": "success",
                        "attempts": attempts_used,
                        "duration_sec": round(duration, 2),
                    }
                )

                self.after(0, lambda j=render_job: self._update_job_row(j))
                self.after(0, self._update_progress)

            if self.cancel_requested:
                self.after(0, lambda: self._set_status("Cancelled"))
                return

            failed_count = len([job for job in self.jobs if job.id in job_ids and job.status == "failed"])
            if failed_count:
                self.log_queue.put(f"[app] Finished with failures: {failed_count} job(s).")
                self.after(0, lambda: self._set_status("Finished with failures"))
                self.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Render Complete",
                        f"Render run completed with {failed_count} failed job(s).",
                    ),
                )
            else:
                self.log_queue.put("[app] All renders finished.")
                self.after(0, lambda: self._set_status("Done"))
                self.after(0, lambda: messagebox.showinfo("Complete", "All render jobs finished successfully."))

            total_duration = round(time.time() - run_started, 2)
            self.last_run_report_path = self._write_run_report(report_jobs, total_duration)

        except Exception as exc:
            self.log_queue.put(f"[app] ERROR: {exc}")
            self.after(0, lambda: self._set_status("Failed"))
            self.after(0, lambda: messagebox.showerror("Render Failed", str(exc)))

        finally:
            self.is_rendering = False
            self.active_run_job_ids = []
            self.after(0, lambda: self.render_button.configure(state="normal"))
            self.after(0, self._update_summary)

    def _record_job_log(self, job: RenderJob, line: str) -> None:
        stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {line}"
        job.logs.append(stamped)

        frame = self._extract_frame(line)
        if frame is not None:
            frame_start = int(self.frame_start_var.get())
            frame_end = int(self.frame_end_var.get())
            frame_span = max(1, frame_end - frame_start + 1)
            normalized = ((frame - frame_start) / frame_span) * 100.0
            job.progress = max(0.0, min(100.0, normalized))
            self.after(0, lambda j=job: self._update_job_row(j))
            self.after(0, self._update_progress)

        self.log_queue.put(stamped)

    def _update_progress(self) -> None:
        if not self.active_run_job_ids:
            self.progress_var.set(0.0)
            self.progress_caption_var.set("No render in progress")
            return

        selected = [job for job in self.jobs if job.id in self.active_run_job_ids]
        if not selected:
            self.progress_var.set(0.0)
            return

        aggregate = 0.0
        for job in selected:
            if job.status == "success":
                aggregate += 100.0
            elif job.status == "running":
                aggregate += job.progress
            else:
                aggregate += 0.0

        overall = aggregate / len(selected)
        self.progress_var.set(overall)

        completed = len([job for job in selected if job.status == "success"])
        self.progress_caption_var.set(f"Completed {completed}/{len(selected)} jobs")
        self._update_summary()

    def _update_summary(self) -> None:
        total = len(self.jobs)
        success = len([job for job in self.jobs if job.status == "success"])
        failed = len([job for job in self.jobs if job.status == "failed"])
        running = len([job for job in self.jobs if job.status == "running"])

        if self.material_assignments:
            prefix = f"Composite render | {len(self.material_assignments)} mapped materials"
        else:
            prefix = f"Queue: {total} total"

        self.run_summary_var.set(f"{prefix} | {success} success | {failed} failed | {running} running")

    def _write_run_report(self, report_jobs: list[dict], total_duration_sec: float) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_dir = Path.home() / ".blender_video_mapper" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_json = report_dir / f"run_report_{timestamp}.json"
        report_txt = report_dir / f"run_report_{timestamp}.txt"

        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": self.status_var.get(),
            "scene": self.scene_path_var.get().strip(),
            "output_profile": self.output_profile_var.get().strip(),
            "total_duration_sec": total_duration_sec,
            "jobs": report_jobs,
        }
        report_json.write_text(json.dumps(payload, indent=2))

        lines = [
            f"Created: {payload['created_at']}",
            f"Scene: {payload['scene']}",
            f"Profile: {payload['output_profile']}",
            f"Total duration (sec): {total_duration_sec}",
            "",
            "Jobs:",
        ]
        for job in report_jobs:
            label = job.get("job") or Path(job.get("video", "")).name or "job"
            lines.append(
                f"- {label}: {job.get('status')} "
                f"attempts={job.get('attempts')} duration={job.get('duration_sec')}s output={job.get('output')}"
            )
        report_txt.write_text("\n".join(lines) + "\n")

        self.log_queue.put(f"[app] Run report written: {report_json}")
        return str(report_json)

    @staticmethod
    def _last_worker_error_hint(job: RenderJob) -> str:
        for line in reversed(job.logs):
            lowered = line.lower()
            if "[worker] error" in lowered or "traceback" in lowered or "error" in lowered:
                return line
        return ""

    @staticmethod
    def _extract_frame(line: str) -> int | None:
        for pattern in FRAME_PATTERNS:
            match = pattern.search(line)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    return None
        return None

    def _retry_failed_jobs(self) -> None:
        if self.is_rendering:
            return

        failed_ids = [job.id for job in self.jobs if job.status in {"failed", "cancelled"}]
        if not failed_ids:
            messagebox.showinfo("Nothing To Retry", "No failed or cancelled jobs available.")
            return

        self.is_rendering = True
        self.cancel_requested = False
        self.active_run_job_ids = failed_ids

        self.render_button.configure(state="disabled")
        self._set_status("Retrying failed jobs...")
        self.progress_var.set(0.0)
        self.progress_caption_var.set(f"Retrying {len(failed_ids)} job(s)")
        self._schedule_profile_save()

        for job in self.jobs:
            if job.id in failed_ids:
                job.progress = 0.0
                self._update_job_row(job)

        threading.Thread(target=self._render_worker, args=(failed_ids,), daemon=True).start()

    def _cancel_current_job(self) -> None:
        if not self.is_rendering:
            messagebox.showinfo("Not Rendering", "No active render to cancel.")
            return

        self.cancel_requested = True
        self._set_status("Cancelling...")
        self.log_queue.put("[app] Cancel requested by user.")

    def _export_selected_log(self) -> None:
        selection = self.jobs_tree.selection()
        if not selection:
            messagebox.showinfo("Select Job", "Select a job in the queue first.")
            return

        selected_id = int(selection[0])
        job = next((j for j in self.jobs if j.id == selected_id), None)
        if job is None:
            messagebox.showerror("Error", "Selected job not found.")
            return

        if not job.logs:
            messagebox.showinfo("No Logs", "The selected job has no logs yet.")
            return

        default_name = f"render_job_{job.id}_{slugify_filename(self._job_display_name(job))}.log"
        target = filedialog.asksaveasfilename(
            title="Export Job Log",
            defaultextension=".log",
            initialfile=default_name,
            filetypes=[("Log Files", "*.log"), ("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not target:
            return

        Path(target).write_text("\n".join(job.logs) + "\n")
        messagebox.showinfo("Export Complete", f"Log exported to:\n{target}")

    def _resolve_worker_script(self) -> str:
        return _resolve_runtime_script("blender_worker.py")

    def _resolve_discovery_script(self) -> str:
        return _resolve_runtime_script("blender_discover.py")


def main() -> None:
    """Clean launcher: always use Qt UI via .venv Python, or directly if PySide6 is available."""
    project_root = Path(__file__).resolve().parent

    # If PySide6 is already importable in this interpreter, run directly.
    try:
        from app_qt import run_qt_app  # type: ignore
        run_qt_app()
        return
    except SystemExit:
        return
    except Exception:
        pass

    # Hand off to .venv Python which has PySide6 installed.
    for venv_python in (
        project_root / ".venv" / "bin" / "python3.12",
        project_root / ".venv" / "bin" / "python3",
        project_root / ".venv" / "Scripts" / "python.exe",  # Windows
    ):
        if venv_python.exists():
            qt_entry = project_root / "app_qt.py"
            if qt_entry.exists():
                os.execv(str(venv_python), [str(venv_python), str(qt_entry)])

    # Last resort: show a clear error rather than launching legacy CTK UI.
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _root = _tk.Tk()
        _root.withdraw()
        _mb.showerror(
            "Render Mapper Pro",
            "PySide6 is not installed and no .venv was found.\n\n"
            "Run:  python -m venv .venv && .venv/bin/pip install PySide6",
        )
        _root.destroy()
    except Exception:
        print("ERROR: PySide6 is required. Run: pip install PySide6", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
