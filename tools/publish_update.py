#!/usr/bin/env python3
"""Publish the latest GitHub release to the studio update share so the app can
auto-update (private repo — no GitHub auth needed by clients).

Run on a machine that has `gh` authenticated AND the share mounted:

    python tools/publish_update.py "/Volumes/SPEEDY/RenderMapperPro" [vX.Y.Z]

It downloads the release's platform zips, copies them to the share, and writes
latest.json. Point the app at the same folder in Properties → General → Updates.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "hayhamLT/RenderMapperPro"
ASSET_KEYS = {
    "RenderMapperPro-macOS-arm64.zip": "macos-arm64",
    "RenderMapperPro-macOS-intel.zip": "macos-intel",
    "RenderMapperPro-Windows-x64.zip": "windows-x64",
}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: publish_update.py <share_dir> [tag]")
        sys.exit(1)
    share = Path(sys.argv[1])
    share.mkdir(parents=True, exist_ok=True)
    tag = sys.argv[2] if len(sys.argv) > 2 else subprocess.check_output(
        ["gh", "release", "view", "--repo", REPO, "--json", "tagName", "-q", ".tagName"],
        text=True).strip()
    version = tag.lstrip("v")
    tmp = share / ".dl"
    tmp.mkdir(exist_ok=True)
    subprocess.check_call(["gh", "release", "download", tag, "--repo", REPO,
                           "--dir", str(tmp), "--clobber"])
    assets = {}
    for f in sorted(tmp.iterdir()):
        if f.name in ASSET_KEYS:
            shutil.copy2(f, share / f.name)
            assets[ASSET_KEYS[f.name]] = f.name
    (share / "latest.json").write_text(json.dumps({"version": version, "assets": assets}, indent=2))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"Published {tag} to {share}: {assets}")


if __name__ == "__main__":
    main()
