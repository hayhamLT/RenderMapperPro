"""Friendly filename-convention patterns (core.naming): the regex-free way to
describe how clip names encode metadata. Verifies the pattern compiles, parses
the same fields the original hand-written regex did, and that the live-preview
helper gives useful messages instead of failing silently.
"""
from __future__ import annotations

import re

import pytest

from core.naming import (
    DEFAULT_PATTERN,
    Literal,
    PatternError,
    Token,
    build_pattern,
    compile_pattern,
    lex_pattern,
    preview,
)

# The original hand-written regex this feature replaces — used to prove the
# friendly pattern captures exactly the same fields.
LEGACY_RE = re.compile(
    r"^(?P<prj>[A-Za-z][A-Za-z0-9]*?)_[Dd](?P<day>\d+)_[Ss](?P<setup>\d+)"
    r"_[Aa](?P<asset>\d+)_(?P<screen>[A-Za-z0-9]+)_(?P<type>[A-Za-z0-9]+)_[Vv](?P<version>\d+)$"
)


def test_default_pattern_matches_legacy_regex_fields():
    sample = "Sunset_D12_S03_A005_LeftWall_Loop_V2"
    legacy = LEGACY_RE.match(sample)
    assert legacy is not None
    parsed = compile_pattern(DEFAULT_PATTERN).parse(sample)
    assert parsed == {
        "Project": "Sunset", "Day": 12, "Setup": 3, "Asset": 5,
        "Screen": "LeftWall", "Type": "Loop", "Version": 2,
    }
    # Same captured values as the legacy regex (numbers as ints on our side).
    assert parsed["Project"] == legacy.group("prj")
    assert parsed["Screen"] == legacy.group("screen")
    assert int(legacy.group("day")) == parsed["Day"]
    assert int(legacy.group("version")) == parsed["Version"]


def test_text_fields_capture_hyphens():
    # Real-world codes contain hyphens (TC-MASTER, War-Treaty, Day-1-Pre); a text
    # field captures them whole, still bounded by the '_' separators.
    p = compile_pattern("{ID#}_D{Day#}_{Section}_{Cue}_{Screen}_v{Version#}")
    assert p.parse("80230_D2_War-Treaty_MusicH_TC-MASTER_v001") == {
        "ID": 80230, "Day": 2, "Section": "War-Treaty",
        "Cue": "MusicH", "Screen": "TC-MASTER", "Version": 1,
    }
    parsed = p.parse("50800_D1_Day-1-Pre_Matte-TransA_DJBS_v001")
    assert parsed is not None and parsed["Section"] == "Day-1-Pre"


def test_hyphen_as_separator_still_resolves():
    # A literal hyphen separator still parses (the matcher backtracks even though
    # text fields may now contain hyphens).
    assert compile_pattern("{Code}-{Num#}").parse("ABC-12") == {"Code": "ABC", "Num": 12}


def test_number_fields_are_ints_and_strip_leading_zeros():
    parsed = compile_pattern("{Project}_D{Day#}").parse("Foo_D007")
    assert parsed == {"Project": "Foo", "Day": 7}


def test_prefix_letters_are_case_insensitive_like_legacy():
    # Legacy used [Dd]/[Vv]; the friendly pattern matches literals case-insensitively.
    assert compile_pattern("{P}_d{Day#}_v{Ver#}").parse("Show_D3_V9") == {
        "P": "Show", "Day": 3, "Ver": 9}


def test_extension_is_ignored():
    p = compile_pattern("{Name}_V{Version#}")
    assert p.parse("Clip_V4.mp4") == {"Name": "Clip", "Version": 4}
    assert p.parse("Clip_V4.mov") == {"Name": "Clip", "Version": 4}


def test_non_matching_returns_none():
    p = compile_pattern("{Project}_D{Day#}")
    assert p.parse("nope-no-day") is None
    assert p.parse("Foo_DX") is None          # Day must be a number


def test_optional_field():
    p = compile_pattern("{Name}_V{Version#?}")
    assert p.parse("Clip_V") == {"Name": "Clip"}        # version absent
    assert p.parse("Clip_V7") == {"Name": "Clip", "Version": 7}


def test_field_names_exposed_for_ui_tokens():
    assert compile_pattern(DEFAULT_PATTERN).field_names == [
        "Project", "Day", "Setup", "Asset", "Screen", "Type", "Version"]


# ── error handling / friendly messages ──────────────────────────────────────

def test_duplicate_field_name_is_rejected():
    with pytest.raises(PatternError, match="more than once"):
        compile_pattern("{Day#}_{Day#}")


def test_unbalanced_brace_is_rejected():
    with pytest.raises(PatternError, match="Unbalanced"):
        compile_pattern("{Project_D{Day#}")


def test_pattern_with_no_fields_is_rejected():
    with pytest.raises(PatternError, match="at least one"):
        compile_pattern("just_literal_text")


def test_empty_pattern_is_rejected():
    with pytest.raises(PatternError, match="empty"):
        compile_pattern("   ")


# ── live preview ────────────────────────────────────────────────────────────

def test_preview_ok_returns_fields():
    r = preview(DEFAULT_PATTERN, "Sunset_D12_S03_A005_LeftWall_Loop_V2")
    assert r.ok and r.fields["Screen"] == "LeftWall" and r.fields["Version"] == 2


def test_preview_pinpoints_where_it_diverged():
    # Day should be a number but the sample has letters there.
    r = preview("{Project}_D{Day#}_S{Setup#}", "Sunset_DX_S03")
    assert not r.ok
    assert "Day" in r.error and "number" in r.error.lower()
    assert "Sunset_D" in r.error          # tells the user how far it got


def test_preview_reports_leftover_text():
    r = preview("{Name}_V{Version#}", "Clip_V4_extra")
    assert not r.ok and "leftover" in r.error.lower()


def test_preview_reports_compile_error_not_crash():
    r = preview("{Day#}_{Day#}", "anything")
    assert not r.ok and "more than once" in r.error


def test_preview_prompts_for_sample_when_blank():
    r = preview(DEFAULT_PATTERN, "")
    assert not r.ok and "sample" in r.error.lower()


# ── chip-builder parts (lex / build round-trip) ──────────────────────────────

def test_lex_pattern_decomposes_into_parts():
    parts = lex_pattern("{Project}_D{Day#}")
    assert parts == [
        Token("Project", False, False),
        Literal("_D"),
        Token("Day", True, False),
    ]


def test_lex_build_roundtrip_is_stable():
    for pat in (DEFAULT_PATTERN, "{A}_{B#}", "x{Name?}y", "{Only#}", "lit{T}lit"):
        assert build_pattern(lex_pattern(pat)) == pat


def test_lex_does_not_raise_on_partial_pattern():
    # A half-typed pattern (unbalanced brace) still lexes to literal text.
    parts = lex_pattern("{Project}_D{Day")
    assert build_pattern(parts) == "{Project}_D{Day"


def test_token_render_flags():
    assert Token("Day", True, False).render() == "{Day#}"
    assert Token("Ver", True, True).render() == "{Ver#?}"
    assert Token("Screen", False, False).render() == "{Screen}"


def test_build_pattern_after_edit_is_parseable():
    # Simulate a chip edit: flip Day to optional, then the result still compiles.
    parts = lex_pattern("{Project}_D{Day#}")
    for p in parts:
        if isinstance(p, Token) and p.name == "Day":
            p.optional = True
    pattern = build_pattern(parts)
    assert pattern == "{Project}_D{Day#?}"
    assert compile_pattern(pattern).parse("Foo_D") == {"Project": "Foo"}
