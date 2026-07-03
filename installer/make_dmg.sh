#!/usr/bin/env bash
# Build a polished drag-to-Applications .dmg from the built .app.
# Usage: installer/make_dmg.sh <output.dmg> [path-to.app]
#
# Produces a styled dmg — branded background, the app icon and an Applications
# alias laid out under a "drag here" arrow (via AppleScript/Finder). If the
# styling step fails (e.g. no Finder/window-server session), it falls back to a
# plain dmg so a release build never breaks.
#
# The .app should be signed + notarized before calling this script so Gatekeeper
# opens it without prompts. The script works with unsigned builds too (CI dev).
set -euo pipefail

OUT="${1:?usage: make_dmg.sh <output.dmg> [app]}"
APP="${2:-dist/Render Mapper Pro.app}"
VOL="Render Mapper Pro"
BG="$(cd "$(dirname "$0")" && pwd)/dmg-background.png"

[ -d "$APP" ] || { echo "App not found: $APP" >&2; exit 1; }

detach_retry() {   # the volume is often briefly "Resource busy" right after Finder
    local mnt="$1" i                         # closes it; retry, then force, before failing
    for i in 1 2 3 4 5; do
        hdiutil detach "$mnt" >/dev/null 2>&1 && return 0
        sleep 2
        hdiutil detach "$mnt" -force >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}

bare_dmg() {   # simple, dependency-free, always works
    local stage; stage="$(mktemp -d)/dmg"; mkdir -p "$stage"
    cp -R "$APP" "$stage/"
    ln -s /Applications "$stage/Applications"
    rm -f "$OUT"
    hdiutil create -volname "$VOL" -srcfolder "$stage" -ov -format UDZO "$OUT" >/dev/null
    rm -rf "$(dirname "$stage")"
    echo "Built (plain) $OUT"
}

styled_dmg() {
    local tmpdir tmpdmg mnt appmb
    tmpdir="$(mktemp -d)"; tmpdmg="$tmpdir/rw.dmg"
    appmb="$(du -sm "$APP" | cut -f1)"
    # Empty writable dmg (NOT -srcfolder, which mounts read-only), then copy the
    # app in so we can lay it out and add the background.
    hdiutil detach "/Volumes/$VOL" >/dev/null 2>&1 || true   # clear any stale mount
    # Empty read-write image (no -format: a sized image defaults to UDRW; -format
    # would require a -srcfolder, which mounts read-only).
    hdiutil create -volname "$VOL" -fs HFS+ -size "$((appmb + 80))m" "$tmpdmg" >/dev/null
    # Capture the real mount point (handles a name-collision suffix) and use the
    # actual disk/app names in the AppleScript.
    local attach_out volname appname
    attach_out="$(hdiutil attach "$tmpdmg" -nobrowse -noautoopen)"
    mnt="$(printf '%s\n' "$attach_out" | grep -oE '/Volumes/.+' | tail -1)"
    [ -n "$mnt" ] && [ -d "$mnt" ] || { echo "attach gave no mount point" >&2; return 1; }
    volname="$(basename "$mnt")"
    appname="$(basename "$APP")"
    cp -R "$APP" "$mnt/"
    ln -s /Applications "$mnt/Applications"
    mkdir -p "$mnt/.background"
    cp "$BG" "$mnt/.background/background.png"
    # Run the Finder layout with a watchdog so a wedged Finder can't hang CI.
    osascript <<OSA &
tell application "Finder"
  tell disk "$volname"
    open
    set theWindow to container window
    set current view of theWindow to icon view
    set toolbar visible of theWindow to false
    set statusbar visible of theWindow to false
    set the bounds of theWindow to {200, 120, 860, 520}
    set viewOpts to the icon view options of theWindow
    set arrangement of viewOpts to not arranged
    set icon size of viewOpts to 112
    set text size of viewOpts to 12
    set background picture of viewOpts to file ".background:background.png"
    set position of item "$appname" of theWindow to {165, 215}
    set position of item "Applications" of theWindow to {495, 215}
    update without registering applications
    delay 1
    close
  end tell
end tell
OSA
    local osa_pid=$!
    ( sleep 90; kill "$osa_pid" 2>/dev/null ) >/dev/null 2>&1 &
    local killer=$!
    disown "$killer" 2>/dev/null || true     # no "Terminated" job-control noise
    local osa_rc=0
    wait "$osa_pid" || osa_rc=$?
    kill "$killer" 2>/dev/null || true
    [ "$osa_rc" -eq 0 ] || return 1
    sync
    # Explicit failure on detach/convert: set -e is suppressed inside a function
    # that's tested by && (the caller below), so a bare failure here would NOT
    # abort — it would fall through to "Built (styled)" and ship NO dmg. Guard it.
    detach_retry "$mnt" || return 1
    rm -f "$OUT"
    hdiutil convert "$tmpdmg" -format UDZO -o "$OUT" >/dev/null || return 1
    rm -rf "$tmpdir"
    [ -f "$OUT" ] || return 1
    echo "Built (styled) $OUT"
}

# Try styled; on any failure, clean up a stray mount and fall back to plain.
if [ -f "$BG" ] && styled_dmg; then
    :
else
    echo "Styled dmg unavailable — using a plain dmg." >&2
    hdiutil detach "/Volumes/$VOL" >/dev/null 2>&1 || true
    bare_dmg
fi

# A release must NEVER ship without a real installer. Verify the result is a
# valid disk image; if not, fail loudly so a flaky hdiutil breaks CI instead of
# silently dropping the .dmg and letting the build "succeed" with no installer.
if ! hdiutil imageinfo "$OUT" >/dev/null 2>&1; then
    echo "FATAL: no valid .dmg was produced at $OUT" >&2
    exit 1
fi
echo "Verified $OUT is a valid disk image."
