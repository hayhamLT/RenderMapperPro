"""Group watch-folder clips into render jobs by a filename naming convention.

This implements the "previz assembly" model from the Motion-Graphics naming
spec: each clip filename is parsed into structured fields
(project / day / setup / asset / screen / type / version), then clips are
grouped by ``(setup, asset)`` so every screen of one asset assembles into a
single multi-screen render — the *previz*. The newest version of each screen
wins, and only the chosen content type (``ANIM``) feeds a render.

Pure and UI-free so it can be unit-tested in isolation; the Qt layer calls
``group_clips`` and turns each :class:`AssetGroup` into a queued RenderJob.

Canonical filename (spec v3.0)::

    PRJ001_D01_S01_A017_CENTER_ANIM_V003
    └proj┘ │   │   │    └screen┘ │    └ver┘
           day setup asset       type
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core import naming
from core.logging_setup import get_logger

_log = get_logger(__name__)

# Filenames are described with the friendly, regex-free pattern from core.naming,
# e.g. "{Project}_D{Day#}_S{Setup#}_A{Asset#}_{Screen}_{Type}_V{Version#}" — much
# easier to read/edit per-show than a regex. A raw regex using the legacy named
# groups (prj/day/setup/asset/screen/type/version) is still accepted, so
# previously-saved configs keep working unchanged.
DEFAULT_PATTERN = naming.DEFAULT_PATTERN

# Friendly field name (lowercased) -> canonical ParsedClip key, so a pattern can
# use human names ({Project}, {Version}) and still feed the spec's fields.
_FIELD_ALIASES = {
    "project": "prj", "prj": "prj", "proj": "prj",
    "day": "day",
    "setup": "setup",
    "asset": "asset",
    "screen": "screen",
    "type": "type",
    "version": "version", "ver": "version",
}

# Default output name for an assembled previz. Tokens are filled by
# AssetGroup.output_name(); zero-padding matches the spec's fixed widths.
DEFAULT_OUTPUT_TEMPLATE = "{prj}_D{day}_S{setup}_A{asset}_PREVIZ_V{ver}"

# Only clips of this deliverable type feed a render (STILL/MAP/PRORES/H264 are
# not the motion content). Empty string disables the filter.
DEFAULT_CONTENT_TYPE = "ANIM"


@dataclass(frozen=True)
class ParsedClip:
    """One watch-folder clip decoded into its naming-convention fields."""

    path: str
    prj: str
    day: int
    setup: int
    asset: int
    screen: str
    type: str
    version: int

    @property
    def asset_key(self) -> tuple[str, int, int]:
        """Identity that groups screens into one render: (project, setup, asset)."""
        return (self.prj.upper(), self.setup, self.asset)


@dataclass
class GroupingConfig:
    """User-tunable rules for parsing + assembling previz renders."""

    enabled: bool = False
    pattern: str = DEFAULT_PATTERN
    content_type: str = DEFAULT_CONTENT_TYPE
    output_template: str = DEFAULT_OUTPUT_TEMPLATE
    # screen code -> material name, for when a material isn't literally named
    # after the screen code (e.g. CENTER -> "Center_Screen"). Missing codes fall
    # back to the screen code itself.
    screen_to_material: dict[str, str] = field(default_factory=dict)
    # setup number -> scene file path, so each setup routes to its own scene.
    # Empty / missing → the caller's current scene is used.
    setup_to_scene: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "pattern": self.pattern,
            "content_type": self.content_type,
            "output_template": self.output_template,
            "screen_to_material": dict(self.screen_to_material),
            "setup_to_scene": {str(k): v for k, v in self.setup_to_scene.items()},
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> GroupingConfig:
        d = d or {}
        raw_setup = d.get("setup_to_scene", {}) or {}
        setup_map: dict[int, str] = {}
        for k, v in raw_setup.items():
            try:
                setup_map[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        return cls(
            enabled=bool(d.get("enabled", False)),
            pattern=str(d.get("pattern") or DEFAULT_PATTERN),
            content_type=str(d.get("content_type", DEFAULT_CONTENT_TYPE)),
            output_template=str(d.get("output_template") or DEFAULT_OUTPUT_TEMPLATE),
            screen_to_material={str(k): str(v) for k, v in (d.get("screen_to_material", {}) or {}).items()},
            setup_to_scene=setup_map,
        )


@dataclass
class AssetGroup:
    """One assembled previz: every screen of a single asset, newest version each."""

    prj: str
    day: int
    setup: int
    asset: int
    screens: dict[str, str]   # screen code -> chosen clip path (latest version)
    version: int              # max screen version, used for the output name

    def material_assignments(self, screen_to_material: dict[str, str]) -> list[tuple[str, str]]:
        """(material_name, clip_path) pairs — screen code maps to its material,
        defaulting to the code itself when no override is configured. Sorted by
        screen code for deterministic ordering."""
        out = []
        for screen in sorted(self.screens):
            material = screen_to_material.get(screen, screen)
            out.append((material, self.screens[screen]))
        return out

    def output_name(self, template: str = DEFAULT_OUTPUT_TEMPLATE) -> str:
        """Fill the output template with zero-padded, spec-width tokens. Unknown
        tokens in the template are left untouched rather than raising."""
        tokens = {
            "prj": self.prj,
            "day": f"{self.day:02d}",
            "setup": f"{self.setup:02d}",
            "asset": f"{self.asset:03d}",
            "pv": f"{self.asset:03d}",   # default: previz id mirrors the asset id
            "ver": f"{self.version:03d}",
            "cam": "",
            "look": "",
        }
        return _safe_format(template, tokens)


def _safe_format(template: str, tokens: dict[str, str]) -> str:
    """str.format that leaves unknown {tokens} as-is instead of raising."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return tokens.get(key, m.group(0))
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, template)


def _is_friendly_pattern(pattern: str) -> bool:
    return "{" in pattern and "}" in pattern


def _match_fields(pattern: str, stem: str) -> dict | None:
    """Canonical {prj,day,setup,asset,screen,type,version} values for a clip
    stem, from either a friendly token pattern or a legacy named-group regex.
    None if the stem doesn't match or the pattern is invalid."""
    if _is_friendly_pattern(pattern):
        try:
            compiled = naming.compile_pattern(pattern)
        except naming.PatternError:
            _log.warning("asset-grouping: invalid pattern %r", pattern)
            return None
        parsed = compiled.parse(stem)
        if parsed is None:
            return None
        out: dict = {}
        for fname, value in parsed.items():
            key = _FIELD_ALIASES.get(fname.lower())
            if key:
                out[key] = value
        return out
    try:
        m = re.match(pattern, stem)
    except re.error:
        _log.warning("asset-grouping: invalid pattern %r", pattern)
        return None
    return m.groupdict() if m else None


def parse_clip(path: str, pattern: str = DEFAULT_PATTERN) -> ParsedClip | None:
    """Decode a single clip path's stem into a :class:`ParsedClip`, or None when
    it doesn't match the convention (so non-conforming files are skipped, not
    misfiled). ``pattern`` may be a friendly token pattern or a legacy regex."""
    stem = Path(path).stem
    g = _match_fields(pattern, stem)
    if g is None:
        return None
    try:
        return ParsedClip(
            path=path,
            prj=str(g.get("prj", "") or ""),
            day=int(g.get("day", 0) or 0),
            setup=int(g.get("setup", 0) or 0),
            asset=int(g.get("asset", 0) or 0),
            screen=str(g.get("screen", "") or ""),
            type=str(g.get("type", "") or ""),
            version=int(g.get("version", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


def group_clips(paths: list[str], config: GroupingConfig | None = None) -> list[AssetGroup]:
    """Parse and group a flat list of clip paths into assembled previz groups.

    Steps: parse each path (skipping non-matches), keep only the configured
    content type, bucket by ``(project, setup, asset)``, and within each bucket
    keep the newest version of every screen. Returns groups ordered by
    ``(setup, asset)``.
    """
    cfg = config or GroupingConfig()
    pattern = cfg.pattern or DEFAULT_PATTERN
    want_type = (cfg.content_type or "").strip().upper()

    # bucket key -> {screen -> best ParsedClip so far}
    buckets: dict[tuple[str, int, int], dict[str, ParsedClip]] = {}
    for path in paths:
        clip = parse_clip(path, pattern)
        if clip is None:
            continue
        if want_type and clip.type.upper() != want_type:
            continue
        screen_map = buckets.setdefault(clip.asset_key, {})
        prev = screen_map.get(clip.screen)
        # Newest version per screen wins (ties keep the first seen).
        if prev is None or clip.version > prev.version:
            screen_map[clip.screen] = clip

    groups: list[AssetGroup] = []
    for (prj, setup, asset), screen_map in buckets.items():
        screens = {s: c.path for s, c in screen_map.items()}
        version = max((c.version for c in screen_map.values()), default=0)
        groups.append(AssetGroup(prj=prj, day=_common_day(screen_map),
                                 setup=setup, asset=asset, screens=screens, version=version))

    groups.sort(key=lambda gr: (gr.setup, gr.asset))
    return groups


def _common_day(screen_map: dict[str, ParsedClip]) -> int:
    """Day for the group — they should all share one; take the first clip's."""
    for c in screen_map.values():
        return c.day
    return 0
