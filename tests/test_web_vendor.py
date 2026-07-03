"""Guard the vendored three.js so the web-render backend can never silently slip
back to a live-CDN dependency (which breaks offline / farm / Deadline renders).

Cheap and Chromium-free: it checks the files and the importmap, not a real
render. The end-to-end offline render is exercised manually / in the web-smoke
job. Pairs with tools/vendor_three.py.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "assets" / "vendor" / "three"
SCENE = ROOT / "assets" / "web_scene.html"

REQUIRED = [
    VENDOR / "three.module.js",
    VENDOR / "three.core.js",
    VENDOR / "addons" / "loaders" / "GLTFLoader.js",
    VENDOR / "addons" / "environments" / "RoomEnvironment.js",
    VENDOR / "addons" / "utils" / "BufferGeometryUtils.js",
]
_REMOTE_IMPORT = re.compile(r"""(?:from|import)\s+['"](https?://[^'"]+)['"]""")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip_comments(src: str) -> str:
    """Drop // and /* */ comments so example URLs in docs aren't mistaken for
    real `import … from '<url>'` statements (three's loaders quote sample URLs)."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def test_required_vendor_files_present() -> None:
    missing = [str(p.relative_to(ROOT)) for p in REQUIRED if not p.exists()]
    assert not missing, f"vendored three.js files missing (run tools/vendor_three.py): {missing}"


def test_importmap_is_local_not_cdn() -> None:
    html = SCENE.read_text(encoding="utf-8")
    # the importmap block must resolve three to the vendored copy, not a URL
    block = html.split('type="importmap"', 1)[1].split("</script>", 1)[0]
    assert "./vendor/three/three.module.js" in block
    assert "./vendor/three/addons/" in block
    assert "http://" not in block and "https://" not in block, "importmap must not reference a CDN"


def test_no_vendored_file_imports_from_a_cdn() -> None:
    """The whole module graph must be self-contained — a stray remote import
    would defeat the point on an offline machine."""
    for js in VENDOR.rglob("*.js"):
        remote = _REMOTE_IMPORT.findall(_strip_comments(js.read_text(encoding="utf-8")))
        assert not remote, f"{js.relative_to(ROOT)} imports from a remote URL: {remote}"


def test_launch_enables_local_module_access() -> None:
    from core import web_render as wr

    for cfg in (wr._GPU_LAUNCH, wr._SOFTWARE_LAUNCH):
        args = cfg["args"]
        assert isinstance(args, list)
        assert "--allow-file-access-from-files" in args, (
            "Chromium blocks file://→file:// ES-module imports without this flag"
        )
