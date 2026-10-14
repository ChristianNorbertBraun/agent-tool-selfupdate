"""Unpacking a GitHub source archive without trusting it."""
from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath

from .github import UpdateRefused

MAX_UNPACKED = 80 * 1024 * 1024
MAX_MEMBERS = 5000
VERSION_LINE = re.compile(r'__version__ = "([^"]+)"')


def unpack(data: bytes, dest: Path) -> Path:
    """Unpack a GitHub source archive into `dest` and return its single top directory. Only plain files and
    directories are accepted; absolute paths, `..`, links and devices refuse the whole archive."""
    dest.mkdir(parents=True)
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError):
        raise UpdateRefused("the download is not a valid archive") from None
    with tf:
        members = tf.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise UpdateRefused("the archive has an unexpected number of files")
        tops, total = set(), 0
        for m in members:
            parts = PurePosixPath(m.name).parts
            if not parts or m.name.startswith("/") or ".." in parts:
                raise UpdateRefused(f"unsafe path in archive: {m.name[:60]!r}")
            if not (m.isreg() or m.isdir()):
                raise UpdateRefused(f"unsupported entry in archive: {m.name[:60]!r}")
            tops.add(parts[0])
            total += m.size
        if len(tops) != 1 or total > MAX_UNPACKED:
            raise UpdateRefused("the archive is not one project folder of reasonable size")
        for m in members:
            target = dest.joinpath(*PurePosixPath(m.name).parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(m) as src, open(target, "wb") as out:  # type: ignore[union-attr]
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o100 else 0o644)
    return dest / tops.pop()


def verify_package(top: Path, package: str, version: str) -> None:
    """`top` holds the Python package `package` and its tests, and `package`/version.py says `version`."""
    if not (top / package / "__init__.py").is_file() or not (top / "tests").is_dir():
        raise UpdateRefused(f"the archive does not contain the package {package} and its tests")
    try:
        found = VERSION_LINE.search((top / package / "version.py").read_text())
    except OSError:
        found = None
    if not found or found.group(1) != version:
        raise UpdateRefused(f"{package}/version.py does not say {version}")
