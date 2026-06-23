"""Render progress + live-log flood control parsed from renderer stdout.

Guards the two reported regressions:
  - the queue progress bar sat at 0% for the whole (often long) first frame and
    never reached 100% from parsing — it now advances *within* a frame via
    'X / Y samples' and reaches ~100% on the last frame;
  - Blender's carriage-return 'Fra:…' refreshes flooded the live log — they're
    now throttled (still parsed for progress).

Pure logic (RenderProgress takes an injected clock), so no Qt/Blender needed.
"""
from __future__ import annotations

from workers import (
    RenderProgress,
    extract_frame_number,
    extract_sample_fraction,
    is_transient_progress_line,
)


def test_extract_sample_fraction():
    assert extract_sample_fraction("Fra:1 Mem:89M | Time:00:03 | Rendering 16 / 64 samples") == 0.25
    assert extract_sample_fraction("Fra:1 | Rendering 64 / 64 samples") == 1.0
    assert extract_sample_fraction("Fra:1 | Syncing Camera_5") is None
    assert extract_sample_fraction("[web] Rendering frame 5/250 (Fra:120)") is None  # not 'samples'
    assert extract_sample_fraction("Rendering 5 / 0 samples") is None                # guard /0


def test_transient_progress_line_matches_both_backends():
    assert is_transient_progress_line("Fra:12 Mem:90M | Rendering 1 / 64 samples")
    assert is_transient_progress_line("[web] Rendering frame 5/250 (Fra:120)")
    assert not is_transient_progress_line("Saved: '/tmp/out/0001.png'")
    assert not is_transient_progress_line("[worker] Applied emission video to 'Video'")


def test_progress_moves_off_zero_within_first_frame():
    """A heavy first frame must not sit at 0%: sample progress advances the bar
    even before the frame changes."""
    p = RenderProgress(frame_start=1, frame_end=100)
    _, _, nf = p.update("Fra:1 | Rendering 1 / 64 samples", now=0.0)
    assert nf == 1                       # first frame started
    mid, _, _ = p.update("Fra:1 | Rendering 64 / 64 samples", now=0.1)
    assert mid is not None and mid > 0.0   # advanced within the first frame


def test_progress_is_monotonic_and_reaches_full_on_last_frame():
    fs, fe = 100, 104                     # 5-frame span
    p = RenderProgress(fs, fe)
    seen = []
    now = 0.0
    for f in range(fs, fe + 1):
        for s in (1, 32, 64):
            now += 1.0
            pct, _, _ = p.update(f"Fra:{f} | Rendering {s} / 64 samples", now=now)
            if pct is not None:
                seen.append(pct)
    assert seen == sorted(seen), f"progress went backwards: {seen}"
    assert 0.0 <= seen[0] < 1.0         # starts near zero (no longer pinned at a hard 0)
    assert seen[-1] >= 99.0             # last frame's last sample ≈ 100%


def test_web_line_drives_queue_progress_for_any_frame_range():
    """The web backend's 'Fra:<abs frame>' token must drive the queue bar
    correctly even when the range doesn't start at 1 (the old output index did
    not)."""
    fs, fe = 100, 500
    p = RenderProgress(fs, fe)
    pct_start, _, nf = p.update("[web] Rendering frame 1/401 (Fra:100)", now=0.0)
    assert nf == 100 and pct_start == 0.0
    pct_mid, _, _ = p.update("[web] Rendering frame 201/401 (Fra:300)", now=1.0)
    assert pct_mid is not None and 49.0 <= pct_mid <= 51.0   # frame 300 of 100..500


def test_log_flood_is_throttled_but_real_lines_always_show():
    p = RenderProgress(1, 10, min_log_interval=1.0)
    # A burst of Fra: refreshes within one second: only the first shows.
    _, show_a, _ = p.update("Fra:1 | Syncing Camera_5", now=0.0)
    _, show_b, _ = p.update("Fra:1 | Syncing Camera_4", now=0.2)
    _, show_c, _ = p.update("Fra:1 | Rendering 1 / 64 samples", now=0.4)
    assert show_a is True and show_b is False and show_c is False
    # …past the interval, a progress line is shown again.
    _, show_d, _ = p.update("Fra:1 | Rendering 64 / 64 samples", now=1.5)
    assert show_d is True
    # Non-progress lines (saves, worker info, errors) are never throttled.
    _, show_saved, _ = p.update("Saved: '/tmp/out/0001.png'", now=1.6)
    _, show_err, _ = p.update("Error: something failed", now=1.6)
    assert show_saved is True and show_err is True


def test_frame_number_prefers_absolute_fra_token():
    # Both 'Fra:120' and 'frame 5' are present; the absolute Fra: token wins.
    assert extract_frame_number("[web] Rendering frame 5/250 (Fra:120)") == 120
