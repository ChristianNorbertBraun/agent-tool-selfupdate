"""selfupdate.toml: what a project tells this library about itself. It sits at the top of the project's repository:

    package = "anton"                       # the Python package; `python3 -m anton config-check` must exist
    service = "anton.service"               # the systemd user unit that runs it
    state = ".local/state/son-of-anton"     # relative to the home: running-version, last-update, pause
    test_timeout = 900                      # optional, seconds for the project's unit tests (default 600)

    [library]
    repo = "ChristianNorbertBraun/agent-tool-selfupdate"
    version = "~=1.0"                       # newest release of this library in that range is installed with it
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .version import Range

FILE = "selfupdate.toml"
PACKAGE = "agent_tool_selfupdate"
SLUG_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
PACKAGE_RE = re.compile(r"[a-z][a-z0-9_]{0,40}")
SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,80}\.service")
STATE_RE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


class ManifestError(ValueError):
    """selfupdate.toml is missing or wrong. The text is safe to show."""


@dataclass(frozen=True)
class Manifest:
    package: str
    service: str
    state: str
    library_repo: str
    library_range: Range
    test_timeout: int = 600

    def state_dir(self, root: Path) -> Path:
        return root / self.state


def parse(text: str) -> Manifest:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{FILE} is not valid TOML: {e}") from None
    package, service, state = data.get("package"), data.get("service"), data.get("state")
    if not (isinstance(package, str) and PACKAGE_RE.fullmatch(package)) or package == PACKAGE:
        raise ManifestError(f"{FILE}: package must be a Python package name")
    if not (isinstance(service, str) and SERVICE_RE.fullmatch(service)):
        raise ManifestError(f"{FILE}: service must be a systemd unit like name.service")
    if not (isinstance(state, str) and STATE_RE.fullmatch(state)) or ".." in state.split("/"):
        raise ManifestError(f"{FILE}: state must be a relative path below the home")
    timeout = data.get("test_timeout", 600)
    if not (isinstance(timeout, int) and not isinstance(timeout, bool) and 30 <= timeout <= 3600):
        raise ManifestError(f"{FILE}: test_timeout must be seconds between 30 and 3600")
    lib = data.get("library")
    if not isinstance(lib, dict):
        raise ManifestError(f"{FILE}: a [library] table with repo and version is missing")
    repo, spec = lib.get("repo"), lib.get("version")
    if not (isinstance(repo, str) and SLUG_RE.fullmatch(repo)):
        raise ManifestError(f"{FILE}: library.repo must look like owner/name")
    try:
        rng = Range(spec) if isinstance(spec, str) else Range("")
    except ValueError as e:
        raise ManifestError(f"{FILE}: library.version: {e}") from None
    return Manifest(package, service, state, repo, rng, timeout)


def load(top: Path) -> Manifest:
    try:
        text = (top / FILE).read_text()
    except OSError:
        raise ManifestError(f"the project has no {FILE}") from None
    return parse(text)
