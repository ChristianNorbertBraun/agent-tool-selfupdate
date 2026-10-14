import unittest

from agent_tool_selfupdate import manifest
from agent_tool_selfupdate.version import Range, parse, parse_name, sort_key

from helpers import MANIFEST


class RangeTests(unittest.TestCase):
    def test_compatible_release_up_to_the_next_major(self):
        r = Range("~=1.2")
        self.assertIn((1, 2, 0), r)
        self.assertIn((1, 9, 7), r)
        self.assertNotIn((1, 1, 9), r)
        self.assertNotIn((2, 0, 0), r)

    def test_with_a_patch_level_it_stays_in_the_minor(self):
        r = Range("~=1.2.3")
        self.assertIn((1, 2, 3), r)
        self.assertIn((1, 2, 99), r)
        self.assertNotIn((1, 2, 2), r)
        self.assertNotIn((1, 3, 0), r)

    def test_exact(self):
        r = Range("==1.2.3")
        self.assertEqual([v in r for v in ((1, 2, 2), (1, 2, 3), (1, 2, 4))], [False, True, False])

    def test_anything_else_is_refused(self):
        for bad in ("1.2", ">=1.2", "~=1", "==1.2", "~=1.2; rm", "", "~=a.b"):
            with self.subTest(bad), self.assertRaises(ValueError):
                Range(bad)


class NameTests(unittest.TestCase):
    def test_release_names(self):
        self.assertEqual(parse_name("0.3.0+u1.0.0"), ((0, 3, 0), (1, 0, 0)))
        self.assertEqual(parse_name("0.2.2"), ((0, 2, 2), None))  # installed before this library existed
        for bad in ("current", "0.3.0+1.0.0", "v0.3.0", "0.3.0+u1.0"):
            self.assertIsNone(parse_name(bad), bad)

    def test_order_is_the_project_first_then_the_library(self):
        names = ["0.2.2", "0.3.0+u1.0.0", "0.3.0+u1.0.10", "0.3.0+u1.0.2", "0.10.0+u1.0.0"]
        self.assertEqual(sorted(names, key=sort_key), ["0.2.2", "0.3.0+u1.0.0", "0.3.0+u1.0.2", "0.3.0+u1.0.10",
                                                        "0.10.0+u1.0.0"])

    def test_tags(self):
        self.assertEqual(parse("v1.2.3"), (1, 2, 3))
        self.assertIsNone(parse("v1.2"))


class ManifestTests(unittest.TestCase):
    def test_a_good_manifest(self):
        m = manifest.parse(MANIFEST)
        self.assertEqual((m.package, m.service, m.state, m.library_repo, m.test_timeout),
                         ("app", "app.service", ".local/state/app", "owner/agent-tool-selfupdate", 600))
        self.assertEqual(m.library_range, Range("~=1.0"))

    def test_test_timeout(self):
        m = manifest.parse(MANIFEST.replace("[library]", "test_timeout = 900\n\n[library]"))
        self.assertEqual(m.test_timeout, 900)

    def test_bad_manifests_are_refused(self):
        bad = {
            "package with a path": MANIFEST.replace('package = "app"', 'package = "../app"'),
            "the library itself": MANIFEST.replace('package = "app"', 'package = "agent_tool_selfupdate"'),
            "service": MANIFEST.replace('"app.service"', '"app; rm -rf"'),
            "state outside the home": MANIFEST.replace('".local/state/app"', '"../hermes"'),
            "absolute state": MANIFEST.replace('".local/state/app"', '"/etc"'),
            "no library": MANIFEST.split("[library]")[0],
            "library repo": MANIFEST.replace('repo = "owner/agent-tool-selfupdate"', 'repo = "nope"'),
            "library range": MANIFEST.replace('"~=1.0"', '">=1.0"'),
            "timeout": MANIFEST.replace("[library]", "test_timeout = 5\n\n[library]"),
            "not toml": "package = [",
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(manifest.ManifestError):
                manifest.parse(text)


if __name__ == "__main__":
    unittest.main()
