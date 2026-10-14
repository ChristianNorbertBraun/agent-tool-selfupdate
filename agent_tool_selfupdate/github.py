"""Finding releases on GitHub. Only a release published by the configured login counts: no draft, no pre-release,
a tag vX.Y.Z. That login is the whole trust model; whoever can publish as it decides what gets installed."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .version import TAG_RE, Range, Version, parse

API = "https://api.github.com"
MAX_ARCHIVE = 30 * 1024 * 1024


class UpdateRefused(Exception):
    """The release or the archive is not acceptable. The text is shown to the user."""


@dataclass(frozen=True)
class Source:
    repo: str       # owner/name of the project
    publisher: str  # only releases published by this GitHub login are installed (also for the library)


@dataclass(frozen=True)
class Release:
    repo: str
    tag: str
    version: Version
    notes: str

    @property
    def name(self) -> str:
        return self.tag[1:]

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/archive/refs/tags/{self.tag}.tar.gz"


class _NoAuthElsewhere(urllib.request.HTTPRedirectHandler):
    """A redirect to another host (an archive goes to codeload.github.com) never carries the token along."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not newurl.startswith(f"{API}/"):
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_NoAuthElsewhere)
RETRY_STATUS = (429, 500, 502, 503, 504)


def _get(url: str, limit: int, attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> bytes:
    headers = {"User-Agent": "agent-tool-selfupdate", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith(f"{API}/"):  # CI only (rate limit); never sent anywhere but the API
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(attempts):  # a Pi on wifi drops a lookup now and then
        try:
            with _opener.open(req, timeout=30) as r:
                data = r.read(limit + 1)
            break
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == attempts - 1:
                raise  # 404, 403: retrying does not help
            sleep(2 * (attempt + 1))
        except OSError:
            if attempt == attempts - 1:
                raise
            sleep(2 * (attempt + 1))
    if len(data) > limit:
        raise UpdateRefused("download is too large")
    return data


def get_json(url: str) -> Any:
    return json.loads(_get(url, 2_000_000))


def get_bytes(url: str) -> bytes:
    return _get(url, MAX_ARCHIVE)


def release_from_api(data: Any, repo: str, publisher: str) -> Release:
    if not isinstance(data, dict):
        raise UpdateRefused("GitHub sent something that is not a release")
    tag = data.get("tag_name")
    if not (isinstance(tag, str) and TAG_RE.fullmatch(tag)):
        raise UpdateRefused("the release tag is not vX.Y.Z")
    if data.get("draft") or data.get("prerelease"):
        raise UpdateRefused("the release is a draft or pre-release")
    author = data.get("author") or {}
    login = str(author.get("login", ""))
    if author.get("type") != "User" or login.lower() != publisher.lower():
        raise UpdateRefused(f"the release was not published by {publisher}")
    return Release(repo, tag, parse(tag), str(data.get("body") or "")[:4000])  # type: ignore[arg-type]


def _refusal(what: str, e: Exception) -> UpdateRefused:
    return UpdateRefused(f"could not read {what}: {type(e).__name__}: {str(e)[:100]}")


def find_release(source: Source, fetch: Callable[[str], Any] = get_json, to: str | None = None) -> Release:
    """The newest release of the project, or the one tagged `to`."""
    if to is not None and not TAG_RE.fullmatch(to):
        raise UpdateRefused("--to must look like v1.2.3")
    path = f"releases/tags/{to}" if to else "releases/latest"
    try:
        data = fetch(f"{API}/repos/{source.repo}/{path}")
    except (OSError, ValueError) as e:  # no network, 404 (no release yet), bad JSON
        raise _refusal("the release", e) from None
    return release_from_api(data, source.repo, source.publisher)


def find_in_range(repo: str, publisher: str, rng: Range, fetch: Callable[[str], Any] = get_json) -> Release:
    """The newest acceptable release of `repo` inside `rng`. Releases by anyone else are skipped."""
    try:
        data = fetch(f"{API}/repos/{repo}/releases?per_page=100")
    except (OSError, ValueError) as e:
        raise _refusal(f"the releases of {repo}", e) from None
    found = []
    for item in data if isinstance(data, list) else []:
        try:
            release = release_from_api(item, repo, publisher)
        except UpdateRefused:
            continue
        if release.version in rng:
            found.append(release)
    if not found:
        raise UpdateRefused(f"{repo} has no release by {publisher} in the range {rng.spec}")
    return max(found, key=lambda r: r.version)


def read_file(repo: str, path: str, tag: str, fetch: Callable[[str], Any] = get_json) -> str:
    """One small text file of the repository at a tag, without downloading the archive."""
    try:
        data = fetch(f"{API}/repos/{repo}/contents/{path}?ref={tag}")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise ValueError("not a file")
        return base64.b64decode(str(data.get("content", ""))).decode()
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise _refusal(f"{path} of {tag}", e) from None
