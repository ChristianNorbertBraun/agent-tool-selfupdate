# agent-tool-selfupdate

Lets a small Python service install its own GitHub releases, safely, on a machine where it runs as a systemd user
service. Part of the agent tools on the Raspberry Pi "merlin": [Son of Anton](https://github.com/ChristianNorbertBraun/son-of-anton),
[Bachman](https://github.com/ChristianNorbertBraun/bachman) and [Gregory](https://github.com/ChristianNorbertBraun/gregory) use it.
Python 3.11+, standard library only.

## What an update does

You publish a release of the project (tag `vX.Y.Z`, the same as `<package>/version.py`). Then "update" in the chat
(or `python3 -m <package> update`) runs this:

1. Find the newest release of the project published by **your** GitHub login (no draft, no pre-release) and read its
   `selfupdate.toml`. It names the range of this library the project works with, for example `~=1.0`.
2. Find the newest release of **this library** in that range, also published by you.
3. Download both, refuse anything odd in the archives (links, `..`, absolute paths, a version that does not match the tag),
   and copy the library into the project tree.
4. Run the library's tests and the project's tests in a bubblewrap sandbox that cannot see the service's home (so not
   its credentials). Then the **probe**: the new library, in the sandbox, must still find the releases on GitHub, so a
   broken updater can never lock a service out of its next update. Then the project's `config-check` against the real config.
5. Wait for running jobs (if the project has any), switch `~/current` to `releases/<project>+u<library>` in one
   atomic step, restart the service, and check that it comes up and reports the new release. If not, switch back.

So an update happens when the project has a new release **or** when this library has a new release inside the
project's range. Nothing updates by itself: you ask for it.

## How a project uses it

`selfupdate.toml` at the top of the project's repository:

```toml
package = "anton"                       # the Python package; `python3 -m anton config-check` must exist
service = "anton.service"               # the systemd user unit that runs it (PYTHONPATH=%h/current)
state = ".local/state/son-of-anton"     # relative to the home
test_timeout = 900                      # optional, default 600 seconds

[library]
repo = "ChristianNorbertBraun/agent-tool-selfupdate"
version = "~=1.0"                       # >= 1.0.0 and < 2.0.0
```

Ranges: `~=1.2` = up to the next major, `~=1.2.3` = up to the next minor, `==1.2.3` = exactly.

The library is **not** in the project's repository. The installation puts it next to the project's package. The
project imports it lazily in a small adapter (its own `updater.py`), so its tests and its normal work run without it:

```python
import agent_tool_selfupdate as su

su.mark_running(state, su.release_dir_of(__file__))   # at service start
su.plan(su.Source(repo, publisher), su.Layout())       # for a "check" tool: .summary(), .notes(), .newer
su.spawn("anton")                                      # for an "apply" tool: runs `python3 -m anton update --yes` in its own unit
su.update(source, su.Layout(), su.Deps(running_jobs=..., notify=...), state_dir=state)   # inside that unit
su.paused(state), su.last_result(state)
```

For development and CI, fetch the version the project allows into the checkout (it is in `.gitignore` there):

```sh
python3 -m agent_tool_selfupdate fetch --publisher ChristianNorbertBraun --into .
```

## First installation of a project that did not use this library yet

The old updater of such a project cannot add the library. Run this library directly once, as the service user:

```sh
d=$(mktemp -d) && curl -fsSL https://github.com/ChristianNorbertBraun/agent-tool-selfupdate/archive/refs/tags/v1.0.0.tar.gz | tar xz -C "$d" --strip-components=1
PYTHONPATH="$d" python3 -m agent_tool_selfupdate update --repo <owner/project> --publisher <login>
```

It does everything above except waiting for jobs, so make sure none run. Afterwards the project's own `update` works.

## Trust model

Whoever can publish a release as the configured login on the project's repository or on this one decides what
gets installed. That is the only gate; everything else protects a working installation from a broken release.
A project's release cannot bring its own copy of this library.

## Layout on disk

```
~/releases/<project>+u<library>/   e.g. 0.3.0+u1.0.0; the newest three are kept, plus the current and previous one
~/current -> releases/...
~/<state>/running-version          written by the service at start: the release directory it runs
~/<state>/last-update              result of the last attempt
~/<state>/pause                    exists while an update waits for running jobs
```

## Tests

```sh
python3 -m unittest discover -s tests
```

MIT license.
