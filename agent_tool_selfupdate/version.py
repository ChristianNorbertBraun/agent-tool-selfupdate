"""Versions and version ranges. A release is a tag `vX.Y.Z` whose agent_tool_selfupdate/version.py says the same."""
from __future__ import annotations

import re

__version__ = "1.0.0"
TAG_RE = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
# an installed release directory: the project's version, plus the version of this library bundled with it
NAME_RE = re.compile(r"(\d{1,4}\.\d{1,4}\.\d{1,4})(?:\+u(\d{1,4}\.\d{1,4}\.\d{1,4}))?")
SPEC_RE = re.compile(r"(~=|==)\s*(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?")

Version = tuple[int, int, int]


def parse(text: str) -> Version | None:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); anything else -> None."""
    m = TAG_RE.fullmatch(text if text.startswith("v") else f"v{text}")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def parse_name(name: str) -> tuple[Version, Version | None] | None:
    """'0.3.0+u1.0.0' -> ((0, 3, 0), (1, 0, 0)); '0.2.2' (installed before this library) -> ((0, 2, 2), None)."""
    m = NAME_RE.fullmatch(name)
    if not m:
        return None
    return parse(m.group(1)), (parse(m.group(2)) if m.group(2) else None)  # type: ignore[return-value]


def sort_key(name: str) -> tuple[Version, Version]:
    parsed = parse_name(name)
    if parsed is None:
        return (0, 0, 0), (0, 0, 0)
    return parsed[0], parsed[1] or (0, 0, 0)


def release_name(app: str, lib: str) -> str:
    return f"{app}+u{lib}"


class Range:
    """A version range like pip's: `~=1.2` is >= 1.2.0 and < 2.0.0, `~=1.2.3` is >= 1.2.3 and < 1.3.0,
    `==1.2.3` is exactly that version."""

    def __init__(self, spec: str):
        m = SPEC_RE.fullmatch(spec.strip())
        if not m:
            raise ValueError(f"not a version range: {spec[:40]!r} (use ~=1.2, ~=1.2.3 or ==1.2.3)")
        op, major, minor, patch = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        self.spec = spec.strip()
        self.low: Version = (major, minor, int(patch or 0))
        if op == "==":
            if patch is None:
                raise ValueError("== needs a full version like ==1.2.3")
            self.high: Version = (major, minor, int(patch) + 1)
        elif patch is None:
            self.high = (major + 1, 0, 0)
        else:
            self.high = (major, minor + 1, 0)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Range) and (self.low, self.high) == (other.low, other.high)

    def __hash__(self) -> int:
        return hash((self.low, self.high))

    def __contains__(self, version: Version) -> bool:
        return self.low <= version < self.high

    def __repr__(self) -> str:
        return f"Range({self.spec!r})"
