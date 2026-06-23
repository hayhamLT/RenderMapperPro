"""Friendly filename-convention patterns — a regex-free way to describe how
input clip names encode metadata (project, day, screen, version, …).

Users write a pattern that reads like a real filename instead of a regex:

    {Project}_D{Day#}_S{Setup#}_A{Asset#}_{Screen}_{Type}_V{Version#}

Tokens:
    {Name}     a text field   (letters/digits)                 -> str
    {Name#}    a number field (digits only, leading zeros ok)  -> int
    {Name?}    optional text field
    {Name#?}   optional number field

Everything outside ``{...}`` is literal text matched case-insensitively (so the
``D``/``S``/``A``/``V`` prefixes match either case). This compiles to a regex
with named groups internally — nothing else in the app needs to touch regex.

Pure and UI-free so it can be unit-tested and reused by the panel's live preview
and any downstream consumer (e.g. feeding parsed fields into output-name tokens).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

# A {...} token: a field name (letter then letters/digits/spaces), optional '#'
# (number) and optional '?' (optional). Names are forgiving for humans; they're
# slugified to a safe regex group name internally.
_TOKEN_RE = re.compile(r"\{\s*([A-Za-z][A-Za-z0-9 _]*?)\s*(#?)\s*(\??)\s*\}")


class PatternError(ValueError):
    """A filename pattern that can't be compiled, with a human-readable reason."""


@dataclass(frozen=True)
class Field:
    name: str            # display name, e.g. "Day"
    is_number: bool      # True -> digits only, value parsed to int
    optional: bool       # True -> may be absent
    group: str           # regex group name (slugified, unique)


@dataclass
class CompiledPattern:
    regex: re.Pattern[str]
    fields: list[Field]
    # Ordered (kind, value) segments: ("lit", text) or ("tok", Field). Used for
    # the field list and for pinpointing where a non-matching sample diverged.
    segments: list[tuple[str, object]] = dc_field(default_factory=list)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def parse(self, filename: str) -> dict[str, object] | None:
        """Parse a filename (with or without extension) into {field: value}, or
        None if it doesn't match. Number fields come back as ``int``."""
        stem = _strip_ext(filename)
        m = self.regex.match(stem)
        if not m:
            return None
        out: dict[str, object] = {}
        for f in self.fields:
            raw = m.group(f.group)
            if raw is None:
                continue                      # absent optional field
            out[f.name] = int(raw) if f.is_number else raw
        return out


def _strip_ext(name: str) -> str:
    # Only strip a short, alphanumeric extension (".mp4", ".mov") — never a chunk
    # of the stem that happens to contain a dot.
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", base)
    return base[: m.start()] if m else base


def _slug_group(name: str, used: set[str]) -> str:
    g = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "field"
    if g[0].isdigit():
        g = "f_" + g
    base, n = g, 2
    while g in used:
        g = f"{base}_{n}"
        n += 1
    used.add(g)
    return g


def compile_pattern(pattern: str) -> CompiledPattern:
    """Compile a friendly pattern into a CompiledPattern. Raises PatternError
    (with a friendly message) on an invalid pattern."""
    if not pattern or not pattern.strip():
        raise PatternError("The pattern is empty.")
    # Catch unbalanced braces before token scanning, so the message is specific.
    if pattern.count("{") != pattern.count("}"):
        raise PatternError("Unbalanced { } — every field needs a matching brace.")

    segments: list[tuple[str, object]] = []
    fields: list[Field] = []
    used_groups: set[str] = set()
    seen_names: set[str] = set()
    regex_parts: list[str] = ["^"]
    pos = 0
    for m in _TOKEN_RE.finditer(pattern):
        if m.start() > pos:                       # literal text before this token
            lit = pattern[pos:m.start()]
            segments.append(("lit", lit))
            regex_parts.append(re.escape(lit))
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        is_number = m.group(2) == "#"
        optional = m.group(3) == "?"
        key = name.lower()
        if key in seen_names:
            raise PatternError(f"The field '{name}' is used more than once — give each a unique name.")
        seen_names.add(key)
        f = Field(name=name, is_number=is_number, optional=optional,
                  group=_slug_group(name, used_groups))
        fields.append(f)
        segments.append(("tok", f))
        cls = r"\d+" if is_number else r"[A-Za-z0-9]+"
        piece = f"(?P<{f.group}>{cls})"
        regex_parts.append(f"(?:{piece})?" if optional else piece)
        pos = m.end()
    if pos < len(pattern):                         # trailing literal
        lit = pattern[pos:]
        segments.append(("lit", lit))
        regex_parts.append(re.escape(lit))

    if not fields:
        raise PatternError("Add at least one {field} so the pattern captures something.")

    regex_parts.append("$")
    try:
        regex = re.compile("".join(regex_parts), re.IGNORECASE)
    except re.error as exc:                         # pragma: no cover - guarded above
        raise PatternError(f"Could not build the matcher: {exc}") from exc
    return CompiledPattern(regex=regex, fields=fields, segments=segments)


@dataclass
class PreviewResult:
    ok: bool
    fields: dict[str, object]    # parsed values when ok
    error: str                   # friendly reason when not ok (compile or no-match)


def _describe(seg: tuple[str, object]) -> str:
    kind, val = seg
    if kind == "lit":
        return f'"{val}"'
    assert isinstance(val, Field)
    return f"{'a number' if val.is_number else 'text'} for '{val.name}'"


def preview(pattern: str, sample: str) -> PreviewResult:
    """Never-raising helper for a live UI preview: compile + match ``sample`` and,
    on failure, say *where* it diverged ("stopped after … — expected a number for
    'Day'") so the pattern is easy to fix without understanding regex."""
    try:
        compiled = compile_pattern(pattern)
    except PatternError as exc:
        return PreviewResult(False, {}, str(exc))

    if not sample.strip():
        return PreviewResult(False, {}, "Type a sample filename to preview the match.")

    parsed = compiled.parse(sample)
    if parsed is not None:
        return PreviewResult(True, parsed, "")

    # No full match — find the longest leading run of segments that matches the
    # start of the sample, then report what the next segment expected.
    stem = _strip_ext(sample)
    segs = compiled.segments
    parts: list[str] = ["^"]

    def _seg_regex(seg: tuple[str, object]) -> str:
        kind, val = seg
        if kind == "lit":
            assert isinstance(val, str)
            return re.escape(val)
        assert isinstance(val, Field)
        cls = r"\d+" if val.is_number else r"[A-Za-z0-9]+"
        piece = f"(?:{cls})"
        return f"{piece}?" if val.optional else piece

    matched_upto = 0
    consumed = ""
    for i, seg in enumerate(segs):
        trial = "".join(parts) + _seg_regex(seg)
        m = re.match(trial, stem, re.IGNORECASE)
        if m:
            parts.append(_seg_regex(seg))
            matched_upto = i + 1
            consumed = m.group(0)
        else:
            break

    if matched_upto >= len(segs):
        extra = stem[len(consumed):]
        return PreviewResult(False, {}, f"Matches up to the end, but there's leftover text: \"{extra}\".")
    nxt = _describe(segs[matched_upto])
    where = f' after "{consumed}"' if consumed else " at the start"
    return PreviewResult(False, {}, f"Stopped{where} — expected {nxt}.")


# Shipped as the suggested starting point in the UI.
DEFAULT_PATTERN = "{Project}_D{Day#}_S{Setup#}_A{Asset#}_{Screen}_{Type}_V{Version#}"
