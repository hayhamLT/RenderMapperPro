"""Tests for core.metrics — render-time analytics math."""
from core.metrics import (
    FrameTimer,
    auto_chunk_size,
    estimate_energy_cost,
    estimate_output_bytes,
    percentile,
    predict_total_seconds,
    summarize,
)


def test_auto_chunk_size():
    # 10 min target at 2 s/frame = 300 frames of work per task.
    assert auto_chunk_size(10, 2.0, 1000) == 300
    # Clamped to the available frame count.
    assert auto_chunk_size(10, 2.0, 100) == 100
    # 10 min at 100 s/frame = 6 frames.
    assert auto_chunk_size(10, 100.0, 1000) == 6
    # No timing or no target → 0 so the caller falls back to manual.
    assert auto_chunk_size(5, 0, 100) == 0
    assert auto_chunk_size(0, 2.0, 100) == 0
    # Never below 1 when computable.
    assert auto_chunk_size(0.001, 2.0, 100) == 1


def test_estimate_energy_cost():
    # 600 W for 1 hour = 0.6 kWh; at $0.20/kWh = $0.12.
    kwh, cost = estimate_energy_cost(3600, 600, 0.20)
    assert abs(kwh - 0.6) < 1e-9
    assert abs(cost - 0.12) < 1e-9
    # Scales with machine count.
    kwh4, _ = estimate_energy_cost(3600, 600, 0.20, machines=4)
    assert abs(kwh4 - 2.4) < 1e-9
    # Clamps.
    assert estimate_energy_cost(0, 600, 0.20) == (0.0, 0.0)
    assert estimate_energy_cost(3600, -5, 0.20)[0] == 0.0


def test_estimate_output_bytes():
    # Video scales with frames and quality; bigger quality → bigger file.
    hi = estimate_output_bytes(1920, 1080, 100, is_video=True, quality="HIGH")
    lo = estimate_output_bytes(1920, 1080, 100, is_video=True, quality="LOW")
    assert hi > lo > 0
    # Twice the frames → twice the size.
    assert estimate_output_bytes(1920, 1080, 200, is_video=True) == 2 * estimate_output_bytes(
        1920, 1080, 100, is_video=True)
    # EXR sequence is much heavier than the same PNG sequence.
    exr = estimate_output_bytes(1920, 1080, 50, is_video=False, image_format="OPEN_EXR")
    png = estimate_output_bytes(1920, 1080, 50, is_video=False, image_format="PNG")
    assert exr > png > 0
    # Half render-scale → about a quarter of the pixels.
    full = estimate_output_bytes(1000, 1000, 10, is_video=True, scale_percent=100)
    half = estimate_output_bytes(1000, 1000, 10, is_video=True, scale_percent=50)
    assert abs(half - full / 4) / full < 0.05


def test_percentile_edges():
    assert percentile([], 50) == 0.0
    assert percentile([5.0], 95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_summarize_empty_and_values():
    empty = summarize([])
    assert empty["count"] == 0 and empty["avg"] == 0.0
    s = summarize([2.0, 4.0, 6.0])
    assert s["count"] == 3
    assert s["avg"] == 4.0
    assert s["total"] == 12.0
    assert s["p50"] == 4.0


def test_frame_timer_durations():
    t = FrameTimer()
    # First frame: no sample yet.
    assert t.record(1, 100.0) is None
    # Repeat line for the same frame: still nothing.
    assert t.record(1, 100.5) is None
    # New frame at +2s → frame 1 took ~2s.
    assert t.record(2, 102.0) == 2.0
    # New frame at +3s → frame 2 took 3s.
    assert t.record(3, 105.0) == 3.0
    assert t.samples == [2.0, 3.0]
    assert summarize(t.samples)["avg"] == 2.5


def test_frame_timer_ignores_backwards_time():
    t = FrameTimer()
    t.record(1, 100.0)
    # A non-monotonic timestamp must not produce a negative sample.
    assert t.record(2, 99.0) is None
    assert t.samples == []


def test_predict_total_seconds():
    history = [
        {"scene": "shot.blend", "avg_spf": 2.0},
        {"scene": "shot.blend", "avg_spf": 4.0},
        {"scene": "other.blend", "avg_spf": 99.0},
    ]
    # p75 of [2,4] = 3.5 → *10 frames = 35s
    assert predict_total_seconds(history, "shot.blend", 10) == 35.0
    # No matching scene → None.
    assert predict_total_seconds(history, "missing.blend", 10) is None
    # No frames → None.
    assert predict_total_seconds(history, "shot.blend", 0) is None


def test_predict_falls_back_to_duration_over_frame_count():
    history = [{"scene": "x.blend", "duration": 100.0, "frame_count": 50}]
    # 100/50 = 2.0 s/frame → *25 = 50s
    assert predict_total_seconds(history, "x.blend", 25) == 50.0
