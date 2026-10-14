"""Fake GitHub data for the tests. Nothing here opens a socket."""
from __future__ import annotations

import base64
import io
import tarfile

PUBLISHER = "Owner"
APP_REPO = "owner/app"
LIB_REPO = "owner/agent-tool-selfupdate"
MANIFEST = f'''package = "app"
service = "app.service"
state = ".local/state/app"

[library]
repo = "{LIB_REPO}"
version = "~=1.0"
'''


def api(tag="v0.2.0", login="owner", kind="User", body="Fixes things", **kw):
    return {"tag_name": tag, "draft": False, "prerelease": False, "body": body,
            "author": {"login": login, "type": kind}, **kw}


def tarball(top: str, files: dict[str, str], extra=()) -> bytes:
    """A GitHub-style source archive: one top directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            data = text.encode()
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info in extra:
            tf.addfile(info)
    return buf.getvalue()


def app_archive(version="0.2.0", manifest=MANIFEST, files=None, extra=()):
    files = files if files is not None else {
        "app/__init__.py": "", "app/version.py": f'__version__ = "{version}"\n', "tests/test_x.py": "x = 1\n",
        "selfupdate.toml": manifest}
    return tarball(f"app-{version}", files, extra)


def lib_archive(version="1.0.1", files=None):
    files = files if files is not None else {
        "agent_tool_selfupdate/__init__.py": "", "agent_tool_selfupdate/version.py": f'__version__ = "{version}"\n',
        "tests/test_y.py": "y = 1\n"}
    return tarball(f"agent-tool-selfupdate-{version}", files)


def link(name, target, hard=False):
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
    info.linkname = target
    return info


class FakeGitHub:
    """Answers the three API calls of a plan: the project's release, its selfupdate.toml, the library's releases."""

    def __init__(self, app=None, lib_releases=None, manifest=MANIFEST):
        self.app = app if app is not None else api("v0.2.0")
        self.lib_releases = lib_releases if lib_releases is not None else [
            api("v1.0.0"), api("v1.0.1", body="Library fix"), api("v2.0.0"), api("v1.9.0", prerelease=True),
            api("v1.5.0", login="mallory")]
        self.manifest = manifest
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        if url.startswith(f"https://api.github.com/repos/{APP_REPO}/releases/"):
            return self.app
        if url.startswith(f"https://api.github.com/repos/{APP_REPO}/contents/selfupdate.toml?ref="):
            return {"encoding": "base64", "content": base64.b64encode(self.manifest.encode()).decode()}
        if url == f"https://api.github.com/repos/{LIB_REPO}/releases?per_page=100":
            return self.lib_releases
        raise OSError(f"unexpected url {url}")
