"""Guard that blender_worker._audio_av_args stays byte-identical to
core.utils.ffmpeg_movie_av_args.

The two are hand-duplicated on purpose: blender_worker runs under Blender's
bundled Python and can't import core. This test runs under plain Python, imports
both, and asserts they agree on representative inputs — so any future drift
between the two copies fails CI instead of shipping a subtle audio-mux mismatch.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from core.utils import ffmpeg_movie_av_args


def _worker_audio_av_args():
    # blender_worker does `import bpy` at module top; stub it so we can import the
    # module under plain Python purely to reach the pure _audio_av_args helper.
    sys.modules.setdefault("bpy", MagicMock())
    import blender_worker
    return blender_worker._audio_av_args


@pytest.mark.parametrize("audio_paths, vf", [
    (None, ""),
    ([], "scale=trunc(iw/2)*2:trunc(ih/2)*2"),
    (["/a/clip.mov"], ""),
    (["/a/clip.mov"], "scale=1920:1080"),
    (["/a/one.mov", "/b/two.mp4"], ""),
    (["/a/one.mov", "/b/two.mp4", "/c/three.mkv"], "scale=trunc(iw/2)*2:trunc(ih/2)*2"),
    (["", "/b/two.mp4", None], "fps=30"),   # blanks / None are filtered out
])
def test_audio_av_args_parity(audio_paths, vf):
    worker_fn = _worker_audio_av_args()
    assert worker_fn(audio_paths, vf) == ffmpeg_movie_av_args(audio_paths, vf)
