# -*- mode: python ; coding: utf-8 -*-
import os
import platform
import sys

APP_NAME = "Render Mapper Pro"
APP_VERSION = "1.5.1"


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


a = Analysis(
    ['app_qt.py'],
    pathex=[],
    binaries=_ffmpeg_binaries(),
    # Scripts run by Blender as subprocesses (not imported), plus bundled assets
    # (app .icns lives here too, referenced by the BUNDLE icon below).
    datas=[
        ('blender_worker.py', '.'),
        ('blender_discover.py', '.'),
        ('c4d_worker.py', '.'),
        ('c4d_discover.py', '.'),
        ('assets', 'assets'),
        ('THIRD_PARTY_LICENSES.md', '.'),   # GPL/LGPL notices for bundled ffmpeg + Qt
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    upx_exclude=[],
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
