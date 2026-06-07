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

# Static ffmpeg/ffprobe so the packaged app ships with them. Source and version
# are overridable via env vars (FFMPEG_STATIC_BASE / FFMPEG_STATIC_VERSION).
FFMPEG_STATIC_VERSION="${FFMPEG_STATIC_VERSION:-b6.0}"
FFMPEG_STATIC_BASE="${FFMPEG_STATIC_BASE:-https://github.com/eugeneware/ffmpeg-static/releases/download/${FFMPEG_STATIC_VERSION}}"

detect_plat_arch() {
  local os arch
  case "$(uname -s)" in
    Darwin) os="darwin" ;;
    Linux)  os="linux" ;;
    *)      os="unknown" ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64)  arch="x64" ;;
    arm64|aarch64) arch="arm64" ;;
    *)             arch="$(uname -m)" ;;
  esac
  printf '%s-%s\n' "$os" "$arch"
}

fetch_ffmpeg() {
  local platarch dir tool
  platarch="$(detect_plat_arch)"
  dir="vendor/ffmpeg/${platarch}"
  mkdir -p "$dir"
  for tool in ffmpeg ffprobe; do
    if [[ -x "$dir/$tool" ]]; then
      echo "ffmpeg: $dir/$tool already present — skipping download"
      continue
    fi
    echo "Fetching $tool ($platarch) from ${FFMPEG_STATIC_BASE} ..."
    if ! curl -fsSL -o "$dir/$tool" "${FFMPEG_STATIC_BASE}/${tool}-${platarch}"; then
      echo "WARNING: could not download $tool — building WITHOUT bundled $tool" >&2
      rm -f "$dir/$tool"
      continue
    fi
    chmod +x "$dir/$tool"
    if [[ "$(uname -s)" == "Darwin" ]]; then
      xattr -dr com.apple.quarantine "$dir/$tool" 2>/dev/null || true
      codesign --force -s - "$dir/$tool" >/dev/null 2>&1 || true
    fi
  done
}

fetch_ffmpeg

PYTHON_BIN="$(resolve_python_bin)"
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
