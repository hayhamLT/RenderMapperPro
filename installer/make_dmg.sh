#!/usr/bin/env bash
# Build a drag-to-Applications .dmg from the built .app.
# Usage: installer/make_dmg.sh <output.dmg> [path-to.app]
# Unsigned — Gatekeeper still shows a one-time "unidentified developer" prompt
# (right-click → Open) until the .app is signed + notarized with an Apple cert.
set -euo pipefail

OUT="${1:?usage: make_dmg.sh <output.dmg> [app]}"
APP="${2:-dist/Render Mapper Pro.app}"
VOL="Render Mapper Pro"

[ -d "$APP" ] || { echo "App not found: $APP" >&2; exit 1; }

stage="$(mktemp -d)/dmg"
mkdir -p "$stage"
cp -R "$APP" "$stage/"
ln -s /Applications "$stage/Applications"   # drag-to-install target

rm -f "$OUT"
hdiutil create -volname "$VOL" -srcfolder "$stage" -ov -format UDZO "$OUT" >/dev/null
rm -rf "$(dirname "$stage")"
echo "Built $OUT"
