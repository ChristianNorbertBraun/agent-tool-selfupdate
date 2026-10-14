import os
import shutil
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent_tool_selfupdate import archive, github, updater
from agent_tool_selfupdate.github import Source, UpdateRefused
from agent_tool_selfupdate.updater import Deps, Layout

from helpers import APP_REPO, LIB_REPO, MANIFEST, PUBLISHER, FakeGitHub, api, app_archive, lib_archive, link, tarball

SOURCE = Source(APP_REPO, PUBLISHER)


class ReleaseTests(unittest.TestCase):
    def test_a_release_by_the_owner_is_accepted_case_insensitively(self):
        r = github.release_from_api(api(), APP_REPO, PUBLISHER)
        self.assertEqual((r.tag, r.version, r.name), ("v0.2.0", (0, 2, 0), "0.2.0"))
        self.assertEqual(r.url, "https://github.com/owner/app/archive/refs/tags/v0.2.0.tar.gz")

    def test_everything_else_is_refused(self):
        bad = {"another publisher": api(login="mallory"), "a bot": api(login="owner", kind="Bot"),
               "a draft": {**api(), "draft": True}, "a pre-release": {**api(), "prerelease": True},
               "an odd tag": api(tag="v1.2"), "a tag with a path": api(tag="v1.0.0/../x"), "no tag": {"author": {}},
               "not a dict": ["v1.0.0"]}
        for name, data in bad.items():
            with self.subTest(name), self.assertRaises(UpdateRefused):
                github.release_from_api(data, APP_REPO, PUBLISHER)

    def test_find_release_reports_network_problems_as_refusals(self):
        with self.assertRaises(UpdateRefused):
            github.find_release(SOURCE, fetch=mock.Mock(side_effect=OSError("down")))
        with self.assertRaises(UpdateRefused):
            github.find_release(SOURCE, fetch=lambda url: api(), to="latest; rm -rf")

    def test_the_newest_library_in_range_by_the_publisher_wins(self):
        gh = FakeGitHub()
        r = github.find_in_range(LIB_REPO, PUBLISHER, updater.manifest_mod.parse(MANIFEST).library_range, gh)
        self.assertEqual(r.tag, "v1.0.1")  # not v2.0.0 (next major), v1.9.0 (pre-release) or v1.5.0 (mallory)

    def test_no_library_in_range_is_a_refusal(self):
        gh = FakeGitHub(lib_releases=[api("v2.0.0"), api("v1.5.0", login="mallory")])
        with self.assertRaisesRegex(UpdateRefused, "no release by Owner in the range ~=1.0"):
            github.find_in_range(LIB_REPO, PUBLISHER, updater.manifest_mod.parse(MANIFEST).library_range, gh)

    def test_read_file_needs_a_base64_file(self):
        self.assertEqual(github.read_file(APP_REPO, "selfupdate.toml", "v0.2.0", FakeGitHub()), MANIFEST)
        with self.assertRaises(UpdateRefused):
            github.read_file(APP_REPO, "selfupdate.toml", "v0.2.0", lambda url: [{"name": "a dir"}])

    def test_downloads_retry_dropped_connections_and_server_errors_but_not_client_errors(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"data"
        bad_gateway = urllib.error.HTTPError("https://x", 502, "bad gateway", {}, None)
        with mock.patch.object(github._opener, "open", side_effect=[OSError("dns"), bad_gateway, ok]) as op:
            self.assertEqual(github._get("https://x", 100, sleep=lambda s: None), b"data")
            self.assertEqual(op.call_count, 3)
        err = urllib.error.HTTPError("https://x", 404, "nf", {}, None)
        with mock.patch.object(github._opener, "open", side_effect=err) as op, self.assertRaises(urllib.error.HTTPError):
            github._get("https://x", 100, sleep=lambda s: None)
        self.assertEqual(op.call_count, 1)

    def test_a_token_from_the_environment_only_goes_to_the_api(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"{}"
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0k"}), \
                mock.patch.object(github._opener, "open", return_value=ok) as op:
            github._get("https://api.github.com/repos/o/r", 100)
            github._get("https://github.com/o/r/archive/refs/tags/v1.0.0.tar.gz", 100)
        sent = [call.args[0].get_header("Authorization") for call in op.call_args_list]
        self.assertEqual(sent, ["Bearer t0k", None])

    def test_a_redirect_away_from_the_api_drops_the_token(self):
        import urllib.request
        handler = github._NoAuthElsewhere()
        req = urllib.request.Request("https://api.github.com/repos/o/r/tarball", headers={"Authorization": "Bearer t0k"})
        away = handler.redirect_request(req, None, 302, "Found", {}, "https://codeload.github.com/o/r/tar.gz/v1")
        self.assertIsNone(away.get_header("Authorization"))
        same = handler.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/repositories/1/tarball")
        self.assertEqual(same.get_header("Authorization"), "Bearer t0k")


class PlanTests(unittest.TestCase):
    def test_target_and_newer(self):
        layout = Layout(Path("/nonexistent"))
        cases = {None: True, "0.1.0": True, "0.2.0": True, "0.2.0+u1.0.0": True, "0.2.0+u1.0.1": False,
                 "0.2.0+u1.1.0": False, "0.3.0+u1.0.0": False}
        for installed, newer in cases.items():
            p = updater.plan(SOURCE, layout, FakeGitHub(), installed=installed)
            self.assertEqual(p.target, "0.2.0+u1.0.1")
            self.assertEqual(p.newer, newer, installed)

    def test_summary_and_notes(self):
        p = updater.plan(SOURCE, Layout(Path("/nonexistent")), FakeGitHub(), installed="0.2.0+u1.0.0")
        self.assertIn("installed 0.2.0+u1.0.0; newest release v0.2.0 with agent-tool-selfupdate v1.0.1", p.summary())
        self.assertIn("NEWER", p.summary())
        self.assertIn("Library fix", p.notes())  # the library changes, so its notes are shown
        p = updater.plan(SOURCE, Layout(Path("/nonexistent")), FakeGitHub(), installed="0.1.0+u1.0.1")
        self.assertNotIn("Library fix", p.notes())

    def test_a_release_without_a_manifest_is_refused(self):
        gh = FakeGitHub(manifest="package = [")
        with self.assertRaisesRegex(UpdateRefused, "not valid TOML"):
            updater.plan(SOURCE, Layout(Path("/nonexistent")), gh, installed="0.1.0")


class UnpackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_normal_archive_is_unpacked_and_verified(self):
        top = archive.unpack(app_archive(), self.tmp / "in")
        archive.verify_package(top, "app", "0.2.0")
        self.assertEqual((top / "app/version.py").read_text(), '__version__ = "0.2.0"\n')

    def test_links_absolute_paths_and_dotdot_refuse_the_whole_archive(self):
        for name, extra in {"symlink": [link("app-0.2.0/l", "/etc/passwd")],
                            "hardlink": [link("app-0.2.0/h", "app/version.py", hard=True)]}.items():
            with self.subTest(name), self.assertRaises(UpdateRefused):
                archive.unpack(app_archive(extra=extra), self.tmp / f"in-{name}")
        with self.assertRaises(UpdateRefused):
            archive.unpack(tarball("app-0.2.0", {"../evil": "x"}), self.tmp / "in-dotdot")
        with self.assertRaises(UpdateRefused):
            archive.unpack(app_archive(extra=[tarfile.TarInfo("/etc/evil")]), self.tmp / "in-absolute")
        self.assertFalse((self.tmp / "evil").exists())

    def test_two_top_directories_garbage_and_wrong_version_are_refused(self):
        import io
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("a/x", "b/y"):
                tf.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
        with self.assertRaises(UpdateRefused):
            archive.unpack(buf.getvalue(), self.tmp / "two")
        with self.assertRaises(UpdateRefused):
            archive.unpack(b"not a tarball", self.tmp / "junk")
        top = archive.unpack(app_archive("0.9.9"), self.tmp / "ver")
        with self.assertRaises(UpdateRefused):
            archive.verify_package(top, "app", "0.2.0")
        top = archive.unpack(tarball("x", {"README.md": "x"}), self.tmp / "other")
        with self.assertRaises(UpdateRefused):
            archive.verify_package(top, "app", "0.2.0")


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.layout = Layout(self.tmp)
        self.state = self.tmp / ".local/state/app"
        old = self.layout.releases / "0.1.0+u1.0.0"
        (old / "app").mkdir(parents=True)
        os.symlink(old, self.layout.current)
        self.restarts, self.logs, self.notified, self.tested = [], [], [], []
        self.gh = FakeGitHub()
        self.app_bytes, self.lib_bytes = app_archive(), lib_archive()
        self.tests_ok = {"lib": True, "app": True}
        self.probe_ok, self.config_ok, self.health = True, True, True

    def deps(self, **kw):
        def get_bytes(url):
            return self.lib_bytes if "agent-tool-selfupdate" in url else self.app_bytes

        def run_tests(top, timeout):
            kind = "app" if (top / "app").exists() else "lib"
            self.tested.append((kind, timeout, sorted(p.name for p in top.iterdir())))
            return self.tests_ok[kind], "1 failed"

        def restart(service):
            self.restarts.append((service, updater.current_name(self.layout)))

        base = dict(
            get_json=self.gh, get_bytes=get_bytes, run_tests=run_tests,
            probe=lambda top, source, target: (self.probe_ok, "cannot reach GitHub"),
            config_check=lambda top, package: (self.config_ok, "bad toml"),
            restart=restart, healthy=lambda name, state, service: self.health if updater.current_name(self.layout) == name
            else True, notify=self.notified.append, log=self.logs.append)
        return Deps(**{**base, **kw})

    def run_update(self, **kw):
        return updater.update(SOURCE, self.layout, self.deps(), **kw)

    def test_a_good_release_is_installed_with_the_library_and_the_old_one_stays(self):
        res = self.run_update()
        self.assertEqual(res.status, "updated", res.message)
        self.assertEqual(updater.current_name(self.layout), "0.2.0+u1.0.1")
        installed = self.layout.releases / "0.2.0+u1.0.1"
        self.assertEqual((installed / "agent_tool_selfupdate/version.py").read_text(), '__version__ = "1.0.1"\n')
        self.assertTrue((installed / "app/__init__.py").is_file())
        self.assertTrue((self.layout.releases / "0.1.0+u1.0.0").is_dir())
        self.assertEqual(self.restarts, [("app.service", "0.2.0+u1.0.1")])
        self.assertEqual([(k, t) for k, t, _ in self.tested], [("lib", 300), ("app", 600)])
        self.assertIn("agent_tool_selfupdate", self.tested[1][2])  # the project's tests run with the library
        self.assertFalse(list(self.layout.releases.glob(".incoming*")))
        self.assertIn("updated: updated 0.1.0+u1.0.0 -> 0.2.0+u1.0.1.", updater.last_result(self.state))
        self.assertNotIn("Fixes things", updater.last_result(self.state))  # release notes never reach a chat agent here
        self.assertEqual(self.notified, ["updated 0.1.0+u1.0.0 -> 0.2.0+u1.0.1. Fixes things"])
        self.assertFalse((self.state / "pause").exists())

    def test_only_a_newer_library_is_an_update_too(self):
        (self.layout.releases / "0.2.0+u1.0.0" / "app").mkdir(parents=True)
        updater.switch(self.layout, "0.2.0+u1.0.0")
        res = self.run_update()
        self.assertEqual((res.status, updater.current_name(self.layout)), ("updated", "0.2.0+u1.0.1"))

    def test_an_installation_from_before_the_library_is_updated(self):
        (self.layout.releases / "0.1.0" / "app").mkdir(parents=True)
        updater.switch(self.layout, "0.1.0")
        res = self.run_update()
        self.assertEqual((res.status, updater.current_name(self.layout)), ("updated", "0.2.0+u1.0.1"))
        self.assertTrue((self.layout.releases / "0.1.0").is_dir())

    def test_failing_checks_change_nothing(self):
        for attr, key, why in (("tests_ok", "lib", "tests of agent-tool-selfupdate v1.0.1 failed"),
                               ("tests_ok", "app", "tests of v0.2.0 failed"),
                               ("probe_ok", None, "new updater could not find the releases"),
                               ("config_ok", None, "cannot read the current config")):
            with self.subTest(why):
                if key:
                    self.tests_ok[key] = False
                else:
                    setattr(self, attr, False)
                res = self.run_update()
                self.tests_ok = {"lib": True, "app": True}
                self.probe_ok = self.config_ok = True
                self.assertEqual(res.status, "failed")
                self.assertIn(why, res.message)
                self.assertEqual(updater.current_name(self.layout), "0.1.0+u1.0.0")
                self.assertEqual(self.restarts, [])
                self.assertFalse((self.layout.releases / "0.2.0+u1.0.1").exists())
                self.assertIn("failed: update refused", updater.last_result(self.state))

    def test_a_failed_health_check_goes_back_to_the_old_version(self):
        self.health = False
        res = self.run_update()
        self.assertEqual(res.status, "rolled-back")
        self.assertEqual(updater.current_name(self.layout), "0.1.0+u1.0.0")
        self.assertEqual([n for _, n in self.restarts], ["0.2.0+u1.0.1", "0.1.0+u1.0.0"])
        self.assertFalse((self.layout.releases / "0.2.0+u1.0.1").exists())
        self.assertIn("went back to 0.1.0+u1.0.0", updater.last_result(self.state))

    def test_a_service_that_will_not_restart_is_rolled_back_too(self):
        calls = []

        def restart(service):
            calls.append(updater.current_name(self.layout))
            if len(calls) == 1:
                raise UpdateRefused("no systemd")

        res = updater.update(SOURCE, self.layout, self.deps(restart=restart))
        self.assertEqual((res.status, updater.current_name(self.layout), calls),
                         ("rolled-back", "0.1.0+u1.0.0", ["0.2.0+u1.0.1", "0.1.0+u1.0.0"]))

    def test_nothing_is_downloaded_when_nothing_is_newer(self):
        (self.layout.releases / "0.2.0+u1.0.1" / "app").mkdir(parents=True)
        updater.switch(self.layout, "0.2.0+u1.0.1")
        get_bytes = mock.Mock()
        res = updater.update(SOURCE, self.layout, self.deps(get_bytes=get_bytes))
        self.assertEqual(res.status, "current")
        get_bytes.assert_not_called()
        self.assertEqual(updater.last_result(self.state), "")  # "nothing new" does not overwrite the last result
        self.assertEqual(self.notified, [])

    def test_force_reinstalls_but_never_the_running_version(self):
        (self.layout.releases / "0.2.0+u1.0.1" / "app").mkdir(parents=True)
        updater.switch(self.layout, "0.2.0+u1.0.1")
        self.assertEqual(self.run_update(force=True).status, "failed")
        self.assertEqual(updater.current_name(self.layout), "0.2.0+u1.0.1")

    def test_a_release_from_the_wrong_publisher_is_refused_and_recorded_where_the_caller_says(self):
        self.gh.app = api(login="mallory")
        res = self.run_update(state_dir=self.tmp / "mystate")
        self.assertEqual(res.status, "failed")
        self.assertIn("not published by Owner", res.message)
        self.assertIn("not published by Owner", updater.last_result(self.tmp / "mystate"))
        self.assertEqual(updater.current_name(self.layout), "0.1.0+u1.0.0")

    def test_archives_that_do_not_match_are_refused(self):
        cases = {
            "the manifest in the archive differs": dict(app=app_archive(manifest=MANIFEST.replace("app.service", "x.service"))),
            "the project ships the library itself": dict(app=app_archive(files={
                "app/__init__.py": "", "app/version.py": '__version__ = "0.2.0"\n', "tests/t.py": "",
                "selfupdate.toml": MANIFEST, "agent_tool_selfupdate/__init__.py": ""})),
            "the library says another version": dict(lib=lib_archive("1.0.0")),
            "no manifest in the archive": dict(app=app_archive(files={
                "app/__init__.py": "", "app/version.py": '__version__ = "0.2.0"\n', "tests/t.py": ""})),
        }
        for name, parts in cases.items():
            with self.subTest(name):
                self.app_bytes, self.lib_bytes = parts.get("app", app_archive()), parts.get("lib", lib_archive())
                res = self.run_update()
                self.assertEqual(res.status, "failed", res.message)
                self.assertEqual(updater.current_name(self.layout), "0.1.0+u1.0.0")
                self.assertEqual(self.tested, [])

    def test_running_jobs_finish_first_and_new_ones_wait(self):
        jobs = [2, 1, 0]
        seen = []

        def running():
            seen.append(updater.paused(self.state))
            return jobs.pop(0)

        res = updater.update(SOURCE, self.layout, self.deps(running_jobs=running, sleep=lambda s: None))
        self.assertEqual(res.status, "updated")
        self.assertEqual(seen, [True, True, True])  # the service saw the pause while it waited
        self.assertFalse(updater.paused(self.state))

    def test_jobs_that_do_not_finish_leave_everything_as_it_was(self):
        clock = {"t": 0.0}
        res = updater.update(SOURCE, self.layout, self.deps(
            running_jobs=lambda: 1, sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"]),
            wait_seconds=60)
        self.assertEqual(res.status, "failed")
        self.assertIn("jobs are still running", res.message)
        self.assertEqual(updater.current_name(self.layout), "0.1.0+u1.0.0")
        self.assertFalse((self.layout.releases / "0.2.0+u1.0.1").exists())
        self.assertFalse(updater.paused(self.state))

    def test_old_versions_are_pruned(self):
        for v in ("0.0.1", "0.0.2+u1.0.0", "0.0.3+u1.0.0"):
            (self.layout.releases / v).mkdir()
        self.run_update()
        left = sorted(p.name for p in self.layout.releases.iterdir() if not p.name.startswith("."))
        self.assertEqual(left, ["0.0.3+u1.0.0", "0.1.0+u1.0.0", "0.2.0+u1.0.1"])

    def test_a_restart_that_blows_up_in_any_way_is_rolled_back(self):
        import subprocess
        calls = []

        def restart(service):
            calls.append(updater.current_name(self.layout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("systemctl", 300)

        res = updater.update(SOURCE, self.layout, self.deps(restart=restart))
        self.assertEqual((res.status, updater.current_name(self.layout)), ("rolled-back", "0.1.0+u1.0.0"))
        self.assertFalse((self.layout.releases / "0.2.0+u1.0.1").exists())

    def test_an_old_running_version_cannot_pass_the_health_check(self):
        self.state.mkdir(parents=True)
        (self.state / "running-version").write_text("0.2.0+u1.0.1")  # left by an earlier attempt
        seen = []
        res = updater.update(SOURCE, self.layout, self.deps(
            healthy=lambda name, state, service: seen.append((state / "running-version").exists()) or True))
        self.assertEqual(res.status, "updated")
        self.assertEqual(seen, [False])

    def test_only_one_update_at_a_time_and_leftovers_of_a_killed_run_are_removed(self):
        import fcntl
        (self.layout.releases / ".incoming-99999" / "app").mkdir(parents=True)
        os.symlink("nowhere", self.tmp / ".current-99999")
        with open(self.layout.releases / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res = self.run_update()
            self.assertEqual(res.status, "failed")
            self.assertIn("another update is running", res.message)
            self.assertTrue((self.layout.releases / ".incoming-99999").exists())
        self.assertEqual(self.run_update().status, "updated")
        self.assertFalse((self.layout.releases / ".incoming-99999").exists())
        self.assertFalse(os.path.lexists(self.tmp / ".current-99999"))

    def test_a_home_without_room_for_releases_is_a_failure_not_a_crash(self):
        (self.tmp / "blocked").write_text("a file, not a directory")
        res = updater.update(SOURCE, Layout(self.tmp / "blocked"), self.deps())
        self.assertEqual(res.status, "failed")

    def test_a_release_that_moves_the_service_or_its_state_is_refused(self):
        (self.layout.current / "selfupdate.toml").write_text(MANIFEST.replace("app.service", "old.service"))
        res = self.run_update()
        self.assertEqual(res.status, "failed")
        self.assertIn("changes the service or state directory", res.message)
        self.assertEqual(updater.current_name(self.layout), "0.1.0+u1.0.0")


class SmallPartsTests(unittest.TestCase):
    def test_healthy_needs_an_active_service_that_reports_the_new_release_for_a_while(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            clock = {"t": 0.0}
            sleep = lambda s: clock.__setitem__("t", clock["t"] + s)  # noqa: E731
            active = mock.Mock(return_value=mock.Mock(stdout="active\n"))
            with mock.patch.object(updater, "systemctl", active):
                (state / "running-version").write_text("0.1.0+u1.0.0")  # still the old service
                self.assertFalse(updater.healthy("0.2.0+u1.0.0", state, "a.service", timeout=30, sleep=sleep,
                                                 clock=lambda: clock["t"]))
                (state / "running-version").write_text("0.2.0+u1.0.0")
                clock["t"] = 0.0
                self.assertTrue(updater.healthy("0.2.0+u1.0.0", state, "a.service", timeout=60, settle=15,
                                                sleep=sleep, clock=lambda: clock["t"]))
            self.assertEqual(active.call_args.args, ("is-active", "a.service"))
            with mock.patch.object(updater, "systemctl", mock.Mock(return_value=mock.Mock(stdout="activating\n"))):
                clock["t"] = 0.0
                self.assertFalse(updater.healthy("0.2.0+u1.0.0", state, "a.service", timeout=30, sleep=sleep,
                                                 clock=lambda: clock["t"]))

    def test_restart_clears_a_start_limit_failure_before_restarting(self):
        calls = []
        ok = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(updater, "systemctl", side_effect=lambda *a, **kw: calls.append(a) or ok):
            updater.restart("app.service")
        self.assertEqual(calls, [("reset-failed", "app.service"), ("restart", "app.service")])
        with mock.patch.object(updater, "systemctl",
                               return_value=mock.Mock(returncode=1, stderr="Start request repeated too quickly")):
            with self.assertRaises(UpdateRefused):
                updater.restart("app.service")

    def test_spawn_runs_the_projects_update_in_its_own_unit(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")) as run, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/srv/state"}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            updater.spawn("app", Path("/home/app"))
        cmd = run.call_args.args[0]
        self.assertIn("--setenv=XDG_STATE_HOME=/srv/state", cmd)
        self.assertFalse(any(a.startswith("--setenv=XDG_CONFIG_HOME") for a in cmd))
        self.assertEqual(cmd[:5], ["systemd-run", "--user", "--collect", "--quiet", "--unit=app-update"])
        self.assertIn("--setenv=PYTHONPATH=/home/app/current", cmd)
        self.assertEqual(cmd[-4:], ["-m", "app", "update", "--yes"])
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stderr="Unit app-update already exists")):
            with self.assertRaisesRegex(UpdateRefused, "already running"):
                updater.spawn("app", Path("/home/app"))

    def test_state_files(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "s"
            self.assertEqual(updater.last_result(state), "")
            self.assertFalse(updater.paused(state))
            updater.mark_running(state, "0.2.0+u1.0.0")
            self.assertEqual((state / "running-version").read_text(), "0.2.0+u1.0.0")
            (state / "pause").touch()
            self.assertTrue(updater.paused(state))
            self.assertFalse(updater.paused(state, now=lambda: os.path.getmtime(state / "pause") + 4 * 3600))

    def test_release_dir_of_a_module(self):
        with tempfile.TemporaryDirectory() as d:
            rel = Path(d) / "releases" / "0.2.0+u1.0.0" / "app"
            rel.mkdir(parents=True)
            os.symlink(rel.parent, Path(d) / "current")
            self.assertEqual(updater.release_dir_of(str(Path(d) / "current/app/updater.py")), "0.2.0+u1.0.0")

    def test_checks_do_not_run_without_a_sandbox(self):
        with mock.patch.object(updater.sandbox, "available", return_value=False), mock.patch("subprocess.run") as run:
            ok, why = updater.run_tests(Path("/nonexistent"), 60)
            self.assertFalse(ok)
            self.assertIn("bwrap", why)
            ok, why = updater.probe(Path("/nonexistent"), SOURCE, "0.2.0+u1.0.0")
            self.assertFalse(ok)
        run.assert_not_called()

    def test_the_probe_runs_the_new_library_in_the_sandbox(self):
        with mock.patch.object(updater.sandbox, "available", return_value=True), \
                mock.patch("subprocess.run", return_value=mock.Mock(
                    returncode=0, stdout="... = 0.2.0+u1.0.1 (already installed).", stderr="")) as run:
            self.assertTrue(updater.probe(Path("/srv/rel"), SOURCE, "0.2.0+u1.0.1")[0])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "bwrap")
        self.assertEqual(cmd[cmd.index("--") + 1:], ["python3", "-m", "agent_tool_selfupdate", "check", "--repo",
                                                     APP_REPO, "--publisher", PUBLISHER, "--installed", "0.2.0+u1.0.1"])
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], "/home/sandbox")
        with mock.patch.object(updater.sandbox, "available", return_value=True), \
                mock.patch("subprocess.run", return_value=mock.Mock(
                    returncode=0, stdout="... = 0.3.0+u1.0.1 (NEWER, can be installed).", stderr="")):
            ok, why = updater.probe(Path("/srv/rel"), SOURCE, "0.2.0+u1.0.1")
        self.assertFalse(ok)  # the new library sees something else than the one that installs it
        self.assertIn("found something else", why)

    def test_systemctl_problems_are_refusals(self):
        import subprocess
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 1)):
            with self.assertRaises(UpdateRefused):
                updater.systemctl("restart", "a.service")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("systemctl")):
            with self.assertRaises(UpdateRefused):
                updater.restart("a.service")

    def test_sandbox_exposes_only_the_release(self):
        args = updater.sandbox.wrap(["true"], Path("/srv/release"))
        binds = [args[i + 1] for i, a in enumerate(args) if a == "--bind"]
        self.assertEqual(binds, ["/srv/release"])
        read_only = [args[i + 1] for i, a in enumerate(args) if a == "--ro-bind"]
        self.assertTrue(all(p == "/usr" or p.startswith("/etc/") for p in read_only), read_only)
        self.assertIn("--unshare-user", args)


class FetchTests(unittest.TestCase):
    def test_fetch_puts_the_library_in_range_next_to_the_project(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "selfupdate.toml").write_text(MANIFEST)
            (root / "agent_tool_selfupdate").mkdir()
            (root / "agent_tool_selfupdate/stale.py").write_text("")
            urls = []
            release = updater.fetch_library(root / "selfupdate.toml", PUBLISHER, root, FakeGitHub(),
                                            lambda url: urls.append(url) or lib_archive())
            self.assertEqual(release.tag, "v1.0.1")
            self.assertEqual(urls, [f"https://github.com/{LIB_REPO}/archive/refs/tags/v1.0.1.tar.gz"])
            self.assertEqual(sorted(p.name for p in (root / "agent_tool_selfupdate").iterdir()),
                             ["__init__.py", "version.py"])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["agent_tool_selfupdate", "selfupdate.toml"])


if __name__ == "__main__":
    unittest.main()
