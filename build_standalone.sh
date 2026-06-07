#!/usr/bin/env bash
set -euo pipefail

resolve_python_bin() {
  if [[ -n "${BLENDER_VIDEO_MAPPER_PYTHON:-}" ]]; then
    if [[ -x "${BLENDER_VIDEO_MAPPER_PYTHON}" ]]; then
      printf '%s\n' "${BLENDER_VIDEO_MAPPER_PYTHON}"
      return 0
    fi
    echo "Configured BLENDER_VIDEO_MAPPER_PYTHON is not executable: ${BLENDER_VIDEO_MAPPER_PYTHON}" >&2
    return 1
  fi

  local candidates=(
    "$HOME/.local/bin/python3.12"
  )

  if command -v python3.12 >/dev/null 2>&1; then
    candidates+=("$(command -v python3.12)")
  fi
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "No usable Python runtime found. Set BLENDER_VIDEO_MAPPER_PYTHON to a Python 3.10+ executable." >&2
  return 1
}

PYTHON_BIN="$(resolve_python_bin)"

# Static ffmpeg/ffprobe so the packaged app ships with them. The fetcher is a
# cross-platform Python script (shared with CI) — single source of truth.
# Source/version are overridable via FFMPEG_STATIC_BASE / FFMPEG_STATIC_VERSION.
"$PYTHON_BIN" tools/fetch_ffmpeg.py

PYTHON_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
BUILD_VENV_DIR=".build-venv-py${PYTHON_VERSION}"
BUILD_PYTHON="$BUILD_VENV_DIR/bin/python"

echo "Using Python runtime: $PYTHON_BIN"
"$PYTHON_BIN" --version

if [[ ! -x "$BUILD_PYTHON" ]]; then
  "$PYTHON_BIN" -m venv "$BUILD_VENV_DIR"
fi

echo "Using build virtualenv: $BUILD_VENV_DIR"
"$BUILD_PYTHON" -m pip install -r requirements.txt

"$BUILD_PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  BlenderVideoMapper.spec

echo "Build complete: 'dist/Render Mapper Pro.app'"
