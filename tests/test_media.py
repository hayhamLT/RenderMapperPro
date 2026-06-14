"""Tests for media.py pure helpers (no ffmpeg required)."""
from media import evenly_spaced


def test_evenly_spaced_basic():
    assert evenly_spaced([], 5) == []
    assert evenly_spaced([1, 2, 3], 0) == []
    # Fewer items than cells → all of them.
    assert evenly_spaced([1, 2, 3], 5) == [1, 2, 3]
    # Exactly cells → all.
    assert evenly_spaced([1, 2, 3, 4], 4) == [1, 2, 3, 4]


def test_evenly_spaced_downsamples_and_includes_first():
    items = list(range(100))
    picks = evenly_spaced(items, 10)
    assert len(picks) == 10
    assert picks[0] == 0
    assert all(0 <= p < 100 for p in picks)
    # Monotonic, evenly spread.
    assert picks == sorted(picks)
    assert picks[-1] >= 80


def test_evenly_spaced_never_indexes_out_of_range():
    # Stress small lists against larger n to confirm the index clamp holds.
    for size in range(1, 20):
        for n in range(1, 25):
            picks = evenly_spaced(list(range(size)), n)
            assert all(0 <= p < size for p in picks)
