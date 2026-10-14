"""python3 -m agent_tool_selfupdate check | update | fetch | version

`check` is what an installation runs inside the sandbox before it switches to a new version (the new code must
still find the releases). `update` installs without the project's hooks: for the first installation of a project
that did not use this library yet, or when the project's own `update` command is broken. `fetch` is for
development and CI only."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import updater
from .github import Source, UpdateRefused
from .version import __version__


def _source(a: argparse.Namespace) -> Source:
    return Source(a.repo, a.publisher)


def cmd_check(a: argparse.Namespace) -> int:
    try:
        p = updater.plan(_source(a), updater.Layout(Path(a.root)), installed=a.installed)
    except UpdateRefused as e:
        print(f"no usable release: {e}", file=sys.stderr)
        return 1
    print(p.summary())
    return 0


def cmd_update(a: argparse.Namespace) -> int:
    layout = updater.Layout(Path(a.root))
    try:
        p = updater.plan(_source(a), layout, to=a.to)
    except UpdateRefused as e:
        print(f"no update: {e}", file=sys.stderr)
        return 1
    print(p.summary())
    if not (p.newer or a.force):
        return 0
    if not a.yes and input(f"install {p.target}? [y/N] ").strip().lower() != "y":
        return 1
    state = Path(a.state_dir) if a.state_dir else None
    result = updater.update(_source(a), layout, updater.Deps(), to=a.to, force=a.force, state_dir=state)
    return 0 if result.status in ("updated", "current") else 1


def cmd_fetch(a: argparse.Namespace) -> int:
    try:
        release = updater.fetch_library(Path(a.manifest), a.publisher, Path(a.into))
    except UpdateRefused as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    print(f"agent-tool-selfupdate {release.tag} -> {Path(a.into) / 'agent_tool_selfupdate'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_tool_selfupdate", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn, text in (("check", cmd_check, "show what would be installed"),
                           ("update", cmd_update, "install the newest release")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--repo", required=True, help="owner/name of the project")
        p.add_argument("--publisher", required=True, help="the GitHub login whose releases count")
        p.add_argument("--root", default=str(Path.home()), help="where releases/ and current live (default: home)")
        p.set_defaults(fn=fn)
        if name == "check":
            p.add_argument("--installed", help="compare against this release name instead of ~/current")
        else:
            p.add_argument("--to", help="a project tag like v1.2.3 instead of the newest release")
            p.add_argument("--force", action="store_true", help="install even if it is not newer")
            p.add_argument("--yes", action="store_true", help="do not ask")
            p.add_argument("--state-dir", help="the service's state directory, if it is not the one in selfupdate.toml")
    f = sub.add_parser("fetch", help="development and CI: put the library version selfupdate.toml allows into a folder")
    f.add_argument("--manifest", default="selfupdate.toml")
    f.add_argument("--publisher", required=True)
    f.add_argument("--into", default=".")
    f.set_defaults(fn=cmd_fetch)
    sub.add_parser("version").set_defaults(fn=lambda _: print(__version__) or 0)
    a = parser.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
