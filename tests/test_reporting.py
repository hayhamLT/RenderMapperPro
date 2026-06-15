"""Tests for the extracted pure reporting helpers."""
from core.reporting import format_duration, friendly_error_hint


def test_friendly_error_hint_matches():
    assert "memory" in friendly_error_hint("CUDA error: out of memory").lower()
    assert "disk" in friendly_error_hint("No space left on device").lower()
    assert "material" in friendly_error_hint("Material not found: Screen").lower()


def test_friendly_error_hint_no_match():
    assert friendly_error_hint("some unrelated gibberish") == ""
    assert friendly_error_hint("") == ""


def test_format_duration():
    assert format_duration(45) == "45s"
    assert format_duration(125) == "2m05s"
    assert format_duration(3725) == "1h02m"
    assert format_duration(-5) == "0s"
