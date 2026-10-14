import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_tool_selfupdate import __main__ as cli, __version__, updater
from agent_tool_selfupdate.github import UpdateRefused

from helpers import APP_REPO, PUBLISHER, FakeGitHub


class CheckTests(unittest.TestCase):
    """`check` is the probe an installation runs with the NEW library: exit 0 only if it found what to install."""

    def run_check(self, plan):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(updater, "plan", plan), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["check", "--repo", APP_REPO, "--publisher", PUBLISHER, "--installed", "0.2.0+u1.0.1"])
        return code, out.getvalue(), err.getvalue()

    def test_a_found_release_is_success_even_if_it_is_installed(self):
        real = updater.plan

        def plan(source, layout, installed=None):
            self.assertEqual((source.repo, source.publisher, installed), (APP_REPO, PUBLISHER, "0.2.0+u1.0.1"))
            return real(source, layout, FakeGitHub(), installed=installed)

        code, out, _ = self.run_check(plan)
        self.assertEqual(code, 0)
        self.assertIn("already installed", out)

    def test_a_refusal_fails_the_probe(self):
        code, _, err = self.run_check(mock.Mock(side_effect=UpdateRefused("could not read the release: URLError")))
        self.assertEqual(code, 1)
        self.assertIn("could not read the release", err)

    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["version"]), 0)
        self.assertEqual(out.getvalue().strip(), __version__)

    def test_version_py_has_the_line_an_installation_checks(self):
        text = (Path(cli.__file__).parent / "version.py").read_text()
        self.assertIn(f'__version__ = "{__version__}"', text)


if __name__ == "__main__":
    unittest.main()
