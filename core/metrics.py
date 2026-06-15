"""Render-time analytics: per-frame timing, summary stats, and ETA prediction.

Pure and UI-free so it can be unit-tested and reused by the worker thread and
the history view alike.
"""
from __future__ import annotations


def percentile(samples: list[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in 0..100). Empty → 0.0."""
    if not samples:
        return 0.0
    s = sorted(samples)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def summarize(samples: list[float]) -> dict[str, float]:
    """Summary stats for a list of per-frame durations (seconds)."""
    if not samples:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "total": 0.0}
    return {
        "count": len(samples),
        "avg": sum(samples) / len(samples),
        "p50": percentile(samples, 50.0),
        "p95": percentile(samples, 95.0),
        "total": sum(samples),
    }


class FrameTimer:
    """Turns a stream of (frame_number, timestamp) observations into per-frame
    durations. A frame's duration is the gap until the *next* distinct frame is
    seen, so the very first frame has no sample until the second arrives."""

    def __init__(self) -> None:
        self.samples: list[float] = []
        self._last_frame: int | None = None
        self._last_t: float | None = None

    def record(self, frame: int, now: float) -> float | None:
        """Observe `frame` being worked on at time `now` (monotonic seconds).
        Returns the duration of the just-completed frame, or None if there isn't
        one yet (first frame, repeat line, or a non-monotonic timestamp)."""
        delta: float | None = None
        if (
            self._last_frame is not None
            and self._last_t is not None
            and frame != self._last_frame
        ):
            d = now - self._last_t
            if d >= 0:
                delta = d
                self.samples.append(d)
        if frame != self._last_frame:
            self._last_frame = frame
            self._last_t = now
        return delta


def auto_chunk_size(target_minutes: float, sec_per_frame: float, frame_count: int) -> int:
    """Frames per Deadline task to target ~``target_minutes`` of work each, given
    measured ``sec_per_frame``. Clamped to [1, frame_count]. Returns 0 when it
    can't be computed (no timing yet) so the caller can fall back to manual."""
    if target_minutes <= 0 or sec_per_frame <= 0 or frame_count <= 0:
        return 0
    frames = int((target_minutes * 60.0) / sec_per_frame)
    return max(1, min(frames, frame_count))


def estimate_energy_cost(duration_s: float, watts: float, rate_per_kwh: float,
                         machines: int = 1) -> tuple[float, float]:
    """From a render duration, return (kWh, cost) for the given draw (watts),
    electricity rate, and machine count. All inputs user-configurable so no
    pricing is baked in. Negative/zero inputs clamp to 0."""
    duration_s = max(0.0, duration_s)
    watts = max(0.0, watts)
    machines = max(1, machines)
    kwh = (watts / 1000.0) * (duration_s / 3600.0) * machines
    return kwh, kwh * max(0.0, rate_per_kwh)


# Rough bits-per-pixel-per-frame for H.264/5 at each quality tier (heuristic).
_VIDEO_BPP = {"LOSSLESS": 0.60, "HIGH": 0.10, "MEDIUM": 0.06, "LOW": 0.03, "LOWEST": 0.02}


def estimate_output_bytes(
    width: int, height: int, frames: int, *,
    is_video: bool, quality: str = "HIGH", image_format: str = "PNG",
    scale_percent: int = 100,
) -> int:
    """Rough output-size estimate in bytes. Deliberately conservative — used to
    warn about low disk space before a render, not for exact accounting."""
    s = max(1, scale_percent) / 100.0
    px = max(1, int(width * s)) * max(1, int(height * s))
    frames = max(1, frames)
    if is_video:
        bpp = _VIDEO_BPP.get(quality.upper(), 0.10)
        return int(px * bpp / 8.0 * frames)
    # Image sequence: EXR (half-float RGBA) is far heavier than compressed PNG.
    per_frame = px * 4 * 2 if image_format.upper() in ("OPEN_EXR", "EXR") else px * 4 // 2
    return int(per_frame * frames)


def predict_total_seconds(
    history: list[dict], scene: str, frame_count: int
) -> float | None:
    """Estimate total render seconds for `frame_count` frames of `scene` from up
    to the 3 most-recent matching history entries (by scene name), using the p75
    of their average sec/frame. Returns None when there's no usable history."""
    if frame_count <= 0:
        return None
    spfs: list[float] = []
    for h in history:   # history is newest-first
        if h.get("scene") != scene and h.get("scene_full") != scene:
            continue
        spf = h.get("avg_spf")
        if not spf:
            dur, frames = h.get("duration"), h.get("frame_count")
            if dur and frames:
                spf = float(dur) / float(frames)
        if spf:
            spfs.append(float(spf))
        if len(spfs) >= 3:
            break
    if not spfs:
        return None
    return percentile(spfs, 75.0) * frame_count
