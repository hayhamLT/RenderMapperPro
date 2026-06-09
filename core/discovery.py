from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .utils import iter_process_output

DISCOVERY_PREFIX = "DISCOVERY_JSON:"
DiscoveryLogCallback = Callable[[str], None]


def parse_discovery_payload(output_lines: list[str]) -> tuple[list[str], list[str], dict]:
    """Pure parser: pull the ``DISCOVERY_JSON:`` payload out of Blender's stdout
    lines and return (materials, cameras, settings). Kept separate from the
    subprocess plumbing so it can be unit-tested without launching Blender."""
    payload_line = None
    for line in output_lines:
        if line.startswith(DISCOVERY_PREFIX):
            payload_line = line
            break
    if payload_line is None:
        raise RuntimeError("Discovery did not return scene data.")
    data = json.loads(payload_line[len(DISCOVERY_PREFIX):])
    materials = list(data.get("materials", []))
    cameras = list(data.get("cameras", []))
    settings = dict(data.get("settings", {}) or {})
    return materials, cameras, settings


def _run_c4d_discovery(c4dpy_executable, discover_script, scene, on_log):
    """Discover a .c4d via c4dpy + the C4D discovery script (license fed on
    stdin). c4dpy needs an absolute script path."""
    c4dpy = os.path.expanduser(c4dpy_executable)
    script = Path(discover_script).expanduser().resolve()
    if on_log:
        on_log("[app] Executing C4D discovery: " + " ".join([c4dpy, str(script), str(scene)]))
    process = subprocess.Popen(
        [c4dpy, str(script), str(scene)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    output_lines: list[str] = []
    if process.stdin:
        process.stdin.write("1\n")  # license method: Maxon App
        process.stdin.flush()
        process.stdin.close()
    if process.stdout is not None:
        for line in process.stdout:
            stripped = line.rstrip()
            output_lines.append(stripped)
            if on_log and not stripped.startswith(DISCOVERY_PREFIX):
                on_log(stripped)
    process.wait()
    return parse_discovery_payload(output_lines)


def discover_scene_elements(
    blender_executable: str,
    discovery_script_path: str,
    scene_path: str,
    on_log: DiscoveryLogCallback | None = None,
    hard_timeout_seconds: int = 0,
    idle_timeout_seconds: int = 0,
    c4dpy_executable: str = "",
    c4d_discover_script: str = "",
) -> tuple[list[str], list[str], dict]:
    # Route Cinema 4D scenes to the C4D discovery backend.
    if str(scene_path).lower().endswith(".c4d") and c4dpy_executable and c4d_discover_script:
        return _run_c4d_discovery(c4dpy_executable, c4d_discover_script,
                                  Path(scene_path).expanduser().resolve(), on_log)

    blender_path = os.path.expanduser(blender_executable)
    script_path = Path(discovery_script_path).expanduser().resolve()
    scene = Path(scene_path).expanduser().resolve()

    if not script_path.exists():
        raise FileNotFoundError(f"Discovery script not found: {script_path}")
    if not scene.exists():
        raise FileNotFoundError(f"Scene file not found: {scene}")

    command = [
        blender_path,
        "-b",
        "--python",
        str(script_path),
        "--",
        str(scene),
    ]

    if on_log:
        on_log("[app] Executing discovery: " + " ".join(command))

    output_lines: list[str] = []

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        if process.stdout is not None:
            def _on_timeout(kind: str, secs: float) -> None:
                if on_log:
                    which = "hard" if kind == "hard" else "idle (no output)"
                    on_log(f"[app] Discovery {which} timeout reached ({secs:g}s), terminating Blender process...")

            for stripped in iter_process_output(
                process,
                hard_timeout=hard_timeout_seconds,
                idle_timeout=idle_timeout_seconds,
                on_timeout=_on_timeout,
            ):
                output_lines.append(stripped)
                if on_log and not stripped.startswith(DISCOVERY_PREFIX):
                    on_log(stripped)

        return_code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()

    if return_code != 0:
        error_text = "\n".join(output_lines[-40:]).strip()
        raise RuntimeError(error_text or f"Discovery failed with exit code {return_code}")

    return parse_discovery_payload(output_lines)
