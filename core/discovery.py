from __future__ import annotations

import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Callable


DISCOVERY_PREFIX = "DISCOVERY_JSON:"
DiscoveryLogCallback = Callable[[str], None]


def discover_scene_elements(
    blender_executable: str,
    discovery_script_path: str,
    scene_path: str,
    on_log: DiscoveryLogCallback | None = None,
    hard_timeout_seconds: int = 0,
    idle_timeout_seconds: int = 0,
) -> tuple[list[str], list[str]]:
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
            started = time.time()
            last_output = started

            while True:
                now = time.time()
                if hard_timeout_seconds > 0 and (now - started) > hard_timeout_seconds:
                    if on_log:
                        on_log(
                            f"[app] Discovery hard timeout reached ({hard_timeout_seconds}s), terminating Blender process..."
                        )
                    process.terminate()
                    break

                if idle_timeout_seconds > 0 and (now - last_output) > idle_timeout_seconds:
                    if on_log:
                        on_log(
                            f"[app] Discovery idle timeout reached ({idle_timeout_seconds}s without output), terminating Blender process..."
                        )
                    process.terminate()
                    break

                if process.poll() is not None:
                    break

                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if not ready:
                    continue

                line = process.stdout.readline()
                if not line:
                    continue

                stripped = line.rstrip()
                last_output = time.time()
                output_lines.append(stripped)
                if on_log and not stripped.startswith(DISCOVERY_PREFIX):
                    on_log(stripped)

            for line in process.stdout:
                stripped = line.rstrip()
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

    payload_line = None
    for line in output_lines:
        if line.startswith(DISCOVERY_PREFIX):
            payload_line = line
            break

    if payload_line is None:
        raise RuntimeError("Discovery did not return scene data.")

    payload = payload_line[len(DISCOVERY_PREFIX) :]
    data = json.loads(payload)

    materials = list(data.get("materials", []))
    cameras = list(data.get("cameras", []))
    return materials, cameras
