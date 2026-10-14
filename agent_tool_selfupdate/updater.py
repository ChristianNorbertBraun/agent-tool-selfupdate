"""The update itself. A project release is published by the owner on GitHub (tag vX.Y.Z); this module installs it
together with the newest release of this library that the project's selfupdate.toml allows.

Trust model: whoever can publish a release as the configured login, on the project's repository and on this
library's. Everything else is about not breaking a working installation: both are unpacked next to the running
version, the library's and the project's tests run in the sandbox (without access to the stored credentials), the
new library proves it can still find releases, the project's config check runs against the real config, running
jobs finish first, the switch is one atomic symlink change, and a failed health check switches back.

On disk (under the home of the service user):
    releases/<project>+u<library>/   one installed version: the project's tree with this package copied into it
    current -> releases/...          what the service runs (PYTHONPATH=~/current)
    <state>/running-version          written by the service at start: the name of the release directory it runs
    <state>/last-update              the last attempt's result, for a chat client that could not wait for it
    <state>/pause                    exists while an update waits for running jobs (see `paused`)
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import archive, github, manifest as manifest_mod, sandbox
from .github import Release, Source, UpdateRefused
from .manifest import PACKAGE, Manifest
from .version import parse_name, release_name, sort_key

KEEP = 3  # installed versions kept on disk (plus the current and previous one)
PAUSE_MAX_AGE = 3 * 3600  # a pause file left behind by a crashed updater stops counting after this
LIBRARY_TEST_TIMEOUT = 300


@dataclass(frozen=True)
class Layout:
    root: Path = field(default_factory=Path.home)

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def current(self) -> Path:
        return self.root / "current"


@dataclass(frozen=True)
class Result:
    status: str  # updated | current | failed | rolled-back
    message: str


# ---- state shared with the running service -----------------------------------------------------------

def release_dir_of(module_file: str) -> str:
    """The name of the release directory a module was loaded from: <release>/<package>/<module>.py."""
    return Path(module_file).resolve().parents[1].name


def mark_running(state: Path, name: str) -> None:
    """The service records which release it runs, so the updater can see the restart worked."""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / "running-version").write_text(name)


def paused(state: Path, now: Callable[[], float] = time.time) -> bool:
    """True while an update waits for running jobs: a service with jobs starts nothing new."""
    try:
        return now() - (state / "pause").stat().st_mtime < PAUSE_MAX_AGE
    except OSError:
        return False


def last_result(state: Path) -> str:
    """What the last update attempt reported."""
    try:
        return (state / "last-update").read_text().strip()[:600]
    except OSError:
        return ""


def current_name(layout: Layout) -> str | None:
    try:
        return layout.current.resolve().name if layout.current.is_symlink() else None
    except OSError:
        return None


# ---- what would be installed --------------------------------------------------------------------------

def is_newer(target: str, installed: str | None) -> bool:
    """Newer project version, or the same project version with a newer library. Never a downgrade."""
    if installed is None or parse_name(installed) is None:
        return True
    return sort_key(target) > sort_key(installed)


@dataclass(frozen=True)
class Plan:
    installed: str | None
    app: Release
    library: Release
    manifest: Manifest

    @property
    def target(self) -> str:
        return release_name(self.app.name, self.library.name)

    @property
    def newer(self) -> bool:
        return is_newer(self.target, self.installed)

    def summary(self) -> str:
        state = "NEWER, can be installed" if self.newer else "already installed"
        return (f"installed {self.installed or 'unknown'}; newest release {self.app.tag} with agent-tool-selfupdate "
                f"{self.library.tag} = {self.target} ({state}).")

    def notes(self, limit: int = 1500) -> str:
        """Release notes of the project, and of the library when that is what changes."""
        text = self.app.notes
        parsed = parse_name(self.installed or "")
        if parsed is None or parsed[1] != self.library.version:
            text += f"\n\nagent-tool-selfupdate {self.library.tag}: {self.library.notes[:400]}"
        return text.strip()[:limit]


def plan(source: Source, layout: Layout, fetch: Callable[[str], Any] = github.get_json, to: str | None = None,
         installed: str | None = None) -> Plan:
    """Find the newest project release (or `to`), read its selfupdate.toml and the newest library release it allows.
    Downloads no archive."""
    app = github.find_release(source, fetch, to)
    try:
        man = manifest_mod.parse(github.read_file(source.repo, manifest_mod.FILE, app.tag, fetch))
    except manifest_mod.ManifestError as e:
        raise UpdateRefused(str(e)) from None
    library = github.find_in_range(man.library_repo, source.publisher, man.library_range, fetch)
    return Plan(installed if installed is not None else current_name(layout), app, library, man)


# ---- checks that run the new code (all but the config check inside the sandbox) -----------------------

def _sandbox_env() -> dict[str, str]:
    return {"PATH": sandbox.SANDBOX_PATH, "HOME": sandbox.SANDBOX_HOME, "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1"}


def run_tests(top: Path, timeout: int) -> tuple[bool, str]:
    """The release's own unit tests, inside the sandbox: nothing of the new code runs unconfined, or near the
    stored credentials, before it passed."""
    if not sandbox.available():
        return False, "bubblewrap (bwrap) is not installed, so the tests cannot run in a sandbox"
    script = "python3 -m unittest discover -s tests 2>&1 | tail -n 5; exit ${PIPESTATUS[0]}"
    try:
        p = subprocess.run(sandbox.wrap(["bash", "-c", script], top), cwd=top, env=_sandbox_env(),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"the tests took longer than {timeout} s"
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:]


def probe(top: Path, source: Source, target: str) -> tuple[bool, str]:
    """The NEW library, inside the sandbox, must still find the releases: otherwise the next update would fail
    and the service could not repair itself."""
    if not sandbox.available():
        return False, "bubblewrap (bwrap) is not installed"
    cmd = ["python3", "-m", PACKAGE, "check", "--repo", source.repo, "--publisher", source.publisher,
           "--installed", target]
    try:
        p = subprocess.run(sandbox.wrap(cmd, top), cwd=top, env=_sandbox_env(), capture_output=True, text=True,
                           timeout=120)
    except subprocess.TimeoutExpired:
        return False, "the check took longer than 120 s"
    # it must not only find something, but the same thing this updater found
    found = f"= {target} (already installed)" in p.stdout
    out = (p.stdout + p.stderr).strip()[-300:]
    return p.returncode == 0 and found, out if found or p.returncode else f"it found something else: {out}"


def config_check(top: Path, package: str) -> tuple[bool, str]:
    """Does the NEW code still understand the real config? (Runs the new code, but only reads the config.)"""
    try:
        p = subprocess.run([sys.executable, "-m", package, "config-check"], cwd=top,
                           env={**os.environ, "PYTHONPATH": str(top), "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "the config check took longer than 60 s"
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:]


# ---- switching and restarting ----------------------------------------------------------------------------

def switch(layout: Layout, name: str) -> None:
    """Point `current` at releases/<name> in one atomic step."""
    tmp = layout.root / f".current-{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(layout.releases / name, tmp)
    os.replace(tmp, layout.current)


def prune(layout: Layout, keep: tuple[str, ...], limit: int = KEEP) -> None:
    versions = sorted((p for p in layout.releases.iterdir() if p.is_dir() and parse_name(p.name)),
                      key=lambda p: sort_key(p.name), reverse=True)
    for p in versions[limit:]:
        if p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)


def systemctl(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    """A timeout or a missing systemctl is an UpdateRefused, so callers deal with one kind of failure."""
    env = {**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}
    try:
        return subprocess.run(["systemctl", "--user", *args], env=env, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as e:
        raise UpdateRefused(f"systemctl {args[0]} failed: {type(e).__name__}") from None


def restart(service: str) -> None:
    systemctl("reset-failed", service)  # a crash-looping release can leave the unit in start-limit-hit: clear it first
    p = systemctl("restart", service, timeout=300)  # stopping waits for running work (TimeoutStopSec)
    if p.returncode != 0:
        raise UpdateRefused(f"could not restart the service: {p.stderr.strip()[:150]}")


def healthy(name: str, state: Path, service: str, timeout: float = 90, settle: float = 15,
            sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """The service is active, says it runs release `name`, and has not crashed again for `settle` seconds."""
    deadline, since = clock() + timeout, None
    while clock() < deadline:
        try:
            says = (state / "running-version").read_text().strip()
        except OSError:
            says = ""
        try:
            active = systemctl("is-active", service, timeout=30).stdout.strip() == "active"
        except UpdateRefused:
            active = False
        if active and says == name:
            since = clock() if since is None else since
            if clock() - since >= settle:
                return True
        else:
            since = None
        sleep(2)
    return False


def spawn(package: str, root: Path | None = None) -> None:
    """Run `python3 -m <package> update --yes` in its own systemd unit, so restarting the service does not kill the
    updater."""
    root = root or Path.home()
    # a service unit may move its config and state with these; the update must look in the same places
    moved = [f"--setenv={name}={os.environ[name]}" for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME") if name in os.environ]
    p = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={package}-update",
                        f"--setenv=PYTHONPATH={root / 'current'}", *moved, f"--working-directory={root}",
                        sys.executable, "-m", package, "update", "--yes"],
                       env={**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")},
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise UpdateRefused("an update is already running" if "already" in p.stderr else "could not start the update")


# ---- the whole update ------------------------------------------------------------------------------------

@dataclass
class Deps:
    get_json: Callable[[str], Any] = github.get_json
    get_bytes: Callable[[str], bytes] = github.get_bytes
    run_tests: Callable[[Path, int], tuple[bool, str]] = run_tests
    probe: Callable[[Path, Source, str], tuple[bool, str]] = probe
    config_check: Callable[[Path, str], tuple[bool, str]] = config_check
    restart: Callable[[str], None] = restart
    healthy: Callable[[str, Path, str], bool] = healthy
    running_jobs: Callable[[], int] = lambda: 0  # a service with jobs reports how many run; the switch waits for 0
    notify: Callable[[str], None] = lambda text: None  # e.g. a chat message with the result
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print


@contextlib.contextmanager
def _exclusive(layout: Layout):
    """One update at a time per installation, and a stop (SIGTERM) still runs the cleanup of the update."""
    layout.releases.mkdir(parents=True, exist_ok=True)
    with open(layout.releases / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateRefused("another update is running") from None
        for left in [*layout.releases.glob(".incoming-*"), *layout.root.glob(".current-*")]:  # from a killed run
            shutil.rmtree(left, ignore_errors=True) if left.is_dir() and not left.is_symlink() else left.unlink(missing_ok=True)
        try:
            old = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        except ValueError:  # not the main thread (tests, an embedding service): no handler
            old = None
        try:
            yield
        finally:
            if old is not None:
                signal.signal(signal.SIGTERM, old)


def update(source: Source, layout: Layout, deps: Deps, to: str | None = None, force: bool = False,
           wait_seconds: float = 1800, state_dir: Path | None = None) -> Result:
    """Install the newest release (or `to`) with the newest library it allows. Never leaves a broken
    installation behind. `state_dir` is where the service keeps its state; without it, the release's
    selfupdate.toml says so (and a refusal before that is read is only logged)."""
    try:
        with _exclusive(layout):
            return _update(source, layout, deps, to, force, wait_seconds, state_dir)
    except UpdateRefused as e:
        deps.log(f"update refused: {e}")
        return Result("failed", f"update refused: {e}")
    except OSError as e:  # e.g. releases/ cannot be created
        deps.log(f"update failed: {e}")
        return Result("failed", f"update failed: {type(e).__name__}: {str(e)[:150]}")


def _update(source: Source, layout: Layout, deps: Deps, to: str | None, force: bool, wait_seconds: float,
            state_dir: Path | None) -> Result:
    state: list[Path] = [state_dir] if state_dir else []

    def done(status: str, message: str, record: bool = True, notes: str = "") -> Result:
        deps.log(message)
        if record and state:  # the chat client cannot wait for the restart; it reads this on its next check
            try:
                state[0].mkdir(parents=True, exist_ok=True, mode=0o700)
                (state[0] / "last-update").write_text(f"{time.strftime('%Y-%m-%d %H:%M')} {status}: {message}"[:600])
            except OSError as e:
                deps.log(f"could not record the result: {e}")
        if record:  # release notes only go to the owner, never into last-update, which a chat agent reads
            deps.notify(f"{message} {notes}".strip())
        return Result(status, message)

    incoming = layout.releases / f".incoming-{os.getpid()}"
    try:
        p = plan(source, layout, deps.get_json, to)
        if not state:
            state.append(p.manifest.state_dir(layout.root))
        if not (p.newer or force):
            return done("current", f"already on {p.installed} (newest: {p.target})", record=False)
        previous = p.installed
        if p.target == previous:
            raise UpdateRefused(f"{p.target} is the installed version")
        target = layout.releases / p.target
        layout.releases.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(incoming, ignore_errors=True)
        incoming.mkdir()

        app = archive.unpack(deps.get_bytes(p.app.url), incoming / "app")
        archive.verify_package(app, p.manifest.package, p.app.name)
        try:
            if manifest_mod.load(app) != p.manifest:
                raise UpdateRefused(f"the archive's {manifest_mod.FILE} differs from the one on GitHub")
        except manifest_mod.ManifestError as e:
            raise UpdateRefused(str(e)) from None
        installed_manifest = _installed_manifest(layout)
        if installed_manifest and (installed_manifest.service, installed_manifest.state) != (p.manifest.service, p.manifest.state):
            raise UpdateRefused("the release changes the service or state directory; install it by hand")
        if (app / PACKAGE).exists():
            raise UpdateRefused(f"the release itself contains {PACKAGE}/; it must come from its own release")
        lib = archive.unpack(deps.get_bytes(p.library.url), incoming / "lib")
        archive.verify_package(lib, PACKAGE, p.library.name)
        shutil.copytree(lib / PACKAGE, app / PACKAGE, ignore=shutil.ignore_patterns("__pycache__"))

        deps.log(f"unpacked {p.target}, running the tests in the sandbox")
        ok, out = deps.run_tests(lib, LIBRARY_TEST_TIMEOUT)
        if not ok:
            raise UpdateRefused(f"the tests of agent-tool-selfupdate {p.library.tag} failed: {out[-200:]}")
        ok, out = deps.run_tests(app, p.manifest.test_timeout)
        if not ok:
            raise UpdateRefused(f"the tests of {p.app.tag} failed: {out[-200:]}")
        ok, out = deps.probe(app, source, p.target)
        if not ok:
            raise UpdateRefused(f"the new updater could not find the releases: {out[-200:]}")
        ok, out = deps.config_check(app, p.manifest.package)
        if not ok:
            raise UpdateRefused(f"{p.app.tag} cannot read the current config: {out[-200:]}")
        shutil.rmtree(target, ignore_errors=True)
        app.rename(target)
        shutil.rmtree(incoming, ignore_errors=True)

        pause = state[0] / "pause"
        state[0].mkdir(parents=True, exist_ok=True, mode=0o700)
        pause.touch()
        try:
            end = deps.clock() + wait_seconds
            while deps.running_jobs() > 0:
                if deps.clock() > end:
                    shutil.rmtree(target, ignore_errors=True)
                    raise UpdateRefused("jobs are still running; nothing was changed, try again later")
                deps.sleep(5)
            service = p.manifest.service
            running = state[0] / "running-version"
            switch(layout, p.target)
            deps.log(f"switched to {p.target}, restarting")
            try:  # whatever goes wrong from here on, the old version comes back
                running.unlink(missing_ok=True)  # only the new process may say it runs
                deps.restart(service)
                good = deps.healthy(p.target, state[0], service)
            except Exception as e:
                deps.log(f"restart of {p.target} failed: {type(e).__name__}: {str(e)[:150]}")
                good = False
            if good:
                try:
                    prune(layout, keep=(p.target, previous or ""))
                except OSError as e:
                    deps.log(f"could not remove old versions: {e}")
                return done("updated", f"updated {previous or 'unknown'} -> {p.target}.", notes=p.app.notes[:300])
            if previous:
                switch(layout, previous)
                try:
                    running.unlink(missing_ok=True)
                    deps.restart(service)
                    back = deps.healthy(previous, state[0], service)
                except Exception:
                    back = False
                shutil.rmtree(target, ignore_errors=True)  # the release that did not work is not kept
                return done("rolled-back", f"{p.target} failed its health check; went back to {previous}"
                            + ("" if back else " (and that one did not come up either: check the service!)"))
            return done("failed", f"{p.target} failed its health check and there is no previous version to go back to")
        finally:
            pause.unlink(missing_ok=True)
    except UpdateRefused as e:
        return done("failed", f"update refused: {e}")
    except Exception as e:  # nothing here may leave a half state without telling
        return done("failed", f"update failed: {type(e).__name__}: {str(e)[:150]}")
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


def _installed_manifest(layout: Layout) -> Manifest | None:
    """The selfupdate.toml of the running version, if it has one (versions before this library have none)."""
    try:
        return manifest_mod.load(layout.current)
    except manifest_mod.ManifestError:
        return None


def fetch_library(manifest_path: Path, publisher: str, into: Path, fetch: Callable[[str], Any] = github.get_json,
                  get_bytes: Callable[[str], bytes] = github.get_bytes) -> Release:
    """For development and CI: put the newest library release the project allows into `into`/agent_tool_selfupdate,
    so the project's tests can import it. Never used for an installation."""
    try:
        man = manifest_mod.parse(manifest_path.read_text())
    except OSError:
        raise UpdateRefused(f"cannot read {manifest_path}") from None
    except manifest_mod.ManifestError as e:
        raise UpdateRefused(str(e)) from None
    release = github.find_in_range(man.library_repo, publisher, man.library_range, fetch)
    work = into / f".{PACKAGE}-{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    try:
        lib = archive.unpack(get_bytes(release.url), work)
        archive.verify_package(lib, PACKAGE, release.name)
        shutil.rmtree(into / PACKAGE, ignore_errors=True)
        shutil.copytree(lib / PACKAGE, into / PACKAGE, ignore=shutil.ignore_patterns("__pycache__"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return release
