"""agent-tool-selfupdate: lets a service on the agent machine install its own GitHub releases.

A project uses it through a small adapter of its own: `plan` for a check, `spawn` to start an update in the
background, `update` (inside that background unit) to install, `mark_running` at service start, and `paused`
/ `last_result` for the service and its chat tools. See README.md."""
from .github import Release, Source, UpdateRefused
from .updater import (Deps, Layout, Plan, Result, last_result, mark_running, paused, plan, release_dir_of, spawn,
                      update)
from .version import __version__

__all__ = ["Deps", "Layout", "Plan", "Release", "Result", "Source", "UpdateRefused", "__version__", "last_result",
           "mark_running", "paused", "plan", "release_dir_of", "spawn", "update"]
