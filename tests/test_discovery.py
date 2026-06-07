import json

import pytest

from core.discovery import parse_discovery_payload


def _payload(materials, cameras, settings=None):
    data = {"materials": materials, "cameras": cameras}
    if settings is not None:
        data["settings"] = settings
    return "DISCOVERY_JSON:" + json.dumps(data)


def test_parse_full_payload():
    lines = [
        "[discover] Loading scene",
        _payload(["Mat", "Screen"], ["Camera"], {"fps": 24, "frame_end": 250}),
        "Blender quit",
    ]
    materials, cameras, settings = parse_discovery_payload(lines)
    assert materials == ["Mat", "Screen"]
    assert cameras == ["Camera"]
    assert settings["fps"] == 24
    assert settings["frame_end"] == 250


def test_parse_without_settings_returns_empty_dict():
    _, _, settings = parse_discovery_payload([_payload([], [])])
    assert settings == {}


def test_parse_missing_payload_raises():
    with pytest.raises(RuntimeError):
        parse_discovery_payload(["just some blender noise", "Blender quit"])
