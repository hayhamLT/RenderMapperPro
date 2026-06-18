# -*- mode: python ; coding: utf-8 -*-
import os
import platform
import sys

APP_NAME = "Render Mapper Pro"


def _app_version():
    """Read the version from app_version.py (single source of truth)."""
    import pathlib
    import re
    txt = pathlib.Path("app_version.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else "0.0.0"


APP_VERSION = _app_version()


def _ffmpeg_binaries():
    """Bundle the static ffmpeg/ffprobe for the current platform so the app
    ships with them. build_standalone.sh fetches these into vendor/ffmpeg/
    before PyInstaller runs; if absent we build without them (the app then
    falls back to a system ffmpeg / its built-in MP4 parser)."""
    sysn = "linux" if sys.platform.startswith("linux") else sys.platform  # darwin|win32|linux
    mach = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(mach, mach)
    vdir = os.path.join("vendor", "ffmpeg", f"{sysn}-{arch}")
    out = []
    for name in ("ffmpeg", "ffprobe"):
        exe = name + (".exe" if sysn == "win32" else "")
        p = os.path.join(vdir, exe)
        if os.path.exists(p):
            out.append((p, "."))  # land at the bundle root (_MEIPASS)
        else:
            print(f"[spec] WARNING: {p} not found — app will be built WITHOUT bundled {name}")
    return out


def _playwright_driver():
    """Bundle Playwright's node driver so the frozen app can run the browser
    installer + the runtime. ``node`` ships via binaries (preserves the +x bit;
    datas strip it); the rest of driver/ as datas. Browsers are NOT bundled —
    they're downloaded on demand into a per-user dir. Returns (binaries, datas)."""
    try:
        import playwright
    except Exception:
        print("[spec] WARNING: playwright not installed — web render backend won't be bundled")
        return [], []
    pw_root = os.path.dirname(playwright.__file__)
    driver = os.path.join(pw_root, "driver")
    node_name = "node.exe" if os.name == "nt" else "node"
    bins, datas = [], []
    for root, _dirs, files in os.walk(driver):
        for f in files:
            src = os.path.join(root, f)
            dest = os.path.join("playwright", os.path.relpath(root, pw_root))
            (bins if f == node_name else datas).append((src, dest))
    return bins, datas


_pw_bins, _pw_datas = _playwright_driver()


def _ca_datas():
    """Land certifi's cacert.pem at the bundle ROOT (alongside the certifi/ copy
    the hook makes), so the frozen app can locate a CA bundle by path even if
    ``import certifi`` fails at runtime — the fix for 'Couldn't reach GitHub'."""
    try:
        import certifi
        return [(certifi.where(), '.')]
    except Exception:
        print("[spec] WARNING: certifi not importable — cacert.pem not bundled at root")
        return []


try:
    from PyInstaller.utils.hooks import collect_submodules
    _pw_hidden = collect_submodules("playwright") + ["greenlet", "pyee"]
except Exception:
    _pw_hidden = []


a = Analysis(
    ['app_qt.py'],
    pathex=[],
    binaries=_ffmpeg_binaries() + _pw_bins,
    # Scripts run by Blender as subprocesses (not imported), plus bundled assets
    # (app .icns lives here too, referenced by the BUNDLE icon below).
    datas=[
        ('blender_worker.py', '.'),
        ('blender_discover.py', '.'),
        ('c4d_worker.py', '.'),
        ('c4d_discover.py', '.'),
        ('assets', 'assets'),
        ('THIRD_PARTY_LICENSES.md', '.'),   # GPL/LGPL notices for bundled ffmpeg + Qt
    ] + _pw_datas + _ca_datas(),
    hiddenimports=_pw_hidden + ['certifi'],   # certifi hook bundles cacert.pem → HTTPS works
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['pw_runtime_hook.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Per-platform executable icon: Windows wants a .ico, macOS uses the .icns on
# the BUNDLE below. Linux ignores the EXE icon (the .desktop file carries it).
_exe_icon = os.path.join("assets", "app_icon.ico") if sys.platform == "win32" else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_exe_icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=['node', 'node.exe'],   # UPX can corrupt the 120MB Playwright node Mach-O
    name=APP_NAME,
)
# BUNDLE only does anything on macOS (it wraps COLLECT into a .app). On
# Windows/Linux the distributable is the COLLECT folder produced above.
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name=f'{APP_NAME}.app',
        icon='assets/app_icon.icns',
        bundle_identifier='com.toyrobotmedia.rendermapperpro',
        version=APP_VERSION,
        info_plist={
            'CFBundleName': APP_NAME,
            'CFBundleDisplayName': APP_NAME,
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'NSHumanReadableCopyright': 'Toy Robot Media',
            'NSHighResolutionCapable': True,
        },
    )
