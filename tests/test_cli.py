#!/usr/bin/env python3
"""Contract tests for the wcag-axe-crawler CLI.

What packaging can break, and what nothing else covers: the crawler imports
Playwright lazily, so every mode except an actual crawl must work on an
interpreter that has never seen a browser, and the documented exit codes are the
tool's CI interface. Nothing here needs a browser, a network or axe-core.

The one test that does crawl (``LiveCrawlTests``) only runs when
``WCAG_DEMO_URL`` is set, because it needs a served site, Playwright and
Chromium.

    python3 -m unittest discover -s tests
    python3 tests/test_cli.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "audit" / "crawl.py"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Some existing file to hand to --axe-path. None of these tests reaches the
#: point where the bundle is read - the JS is injected into the page during a
#: crawl, which needs Playwright - so any real file gets past argument
#: resolution, and using LICENSE means the test leaves nothing behind.
NOT_AN_AXE = REPO / "LICENSE"


def run_cli(*args, extra_env=None, interpreter_args=()):
    env = dict(os.environ)
    env.pop("AXE_CORE_PATH", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-B", *interpreter_args, str(SCRIPT), *args],
        cwd=str(REPO), capture_output=True, text=True, env=env, timeout=300,
    )


def combined(proc):
    return proc.stdout + proc.stderr


class NoBrowserNeededTests(unittest.TestCase):
    """Nothing in this class may need Playwright, a browser or axe-core."""

    def test_help(self):
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("usage:", proc.stdout)
        self.assertIn("--axe-path", proc.stdout)

    def test_url_is_required(self):
        proc = run_cli()
        self.assertEqual(proc.returncode, 2, combined(proc))
        self.assertIn("the following arguments are required: --url", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_non_http_url_is_rejected(self):
        proc = run_cli("--url", "notaurl", "--axe-path", str(NOT_AN_AXE))
        self.assertEqual(proc.returncode, 2, combined(proc))
        self.assertIn("must be an absolute http(s) URL", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_file_url_is_rejected(self):
        proc = run_cli("--url", "file:///etc/passwd", "--axe-path", str(NOT_AN_AXE))
        self.assertEqual(proc.returncode, 2, combined(proc))
        self.assertIn("must be an absolute http(s) URL", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_missing_axe_path_is_refused_not_substituted(self):
        # Used to fall through to the candidate list and silently audit with a
        # different axe-core build, so the rule set in the report was not the
        # one the caller asked for.
        proc = run_cli("--url", "http://127.0.0.1:8765/",
                       "--axe-path", "/root/no-such-axe.min.js")
        self.assertEqual(proc.returncode, 2, combined(proc))
        self.assertIn("--axe-path does not exist", proc.stderr)
        self.assertIn("Refusing to fall back to another axe-core build", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_axe_core_missing_entirely(self):
        # Run in-process with the candidate list emptied, so the test does not
        # depend on whether this machine happens to have axe-core somewhere.
        import contextlib
        import io as _io

        from wcag_axe_crawler import cli

        saved_candidates = cli.AXE_CANDIDATES
        saved_env = os.environ.pop("AXE_CORE_PATH", None)
        saved_cwd = os.getcwd()
        stderr = _io.StringIO()
        cli.AXE_CANDIDATES = ()
        try:
            os.chdir(REPO / "tests")  # no node_modules/axe-core here
            with contextlib.redirect_stderr(stderr):
                rc = cli.main(["--url", "http://127.0.0.1:8765/"])
        finally:
            cli.AXE_CANDIDATES = saved_candidates
            os.chdir(saved_cwd)
            if saved_env is not None:
                os.environ["AXE_CORE_PATH"] = saved_env
        message = stderr.getvalue()
        self.assertEqual(rc, 2, message)
        self.assertIn("axe-core could not be found", message)
        self.assertIn("npm i axe-core", message)
        self.assertNotIn("Traceback", message)

    def test_playwright_missing_is_exit_3_with_instructions(self):
        # -S hides site-packages, so Playwright cannot be imported. The crawl
        # must fail with the install command, not a traceback.
        proc = subprocess.run(
            [sys.executable, "-S", "-B", str(SCRIPT), "--url", "http://127.0.0.1:8765/",
             "--axe-path", str(NOT_AN_AXE)],
            cwd=str(REPO), capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(proc.returncode, 3, combined(proc))
        self.assertIn("Playwright is not installed for this interpreter", proc.stderr)
        self.assertIn("playwright install chromium", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))


class PackageDataTests(unittest.TestCase):
    def test_axe_rule_reference_ships_inside_the_package(self):
        import json

        from wcag_axe_crawler import cli

        reference = Path(cli.__file__).resolve().parent / "axe-rules-reference.json"
        self.assertTrue(reference.is_file(), "not found: %s" % reference)
        data = json.loads(reference.read_text(encoding="utf-8"))
        self.assertEqual(data["axe_core_version"], "4.13.0")
        self.assertEqual(len(data["rules"]), data["rule_count"])
        self.assertGreater(len(data["rules"]), 50)

    def test_playwright_is_not_imported_at_module_scope(self):
        # This is what makes it an optional extra rather than a hard dependency.
        source = Path(
            __import__("wcag_axe_crawler.cli", fromlist=["cli"]).__file__
        ).read_text(encoding="utf-8")
        top_level = source.split("\ndef ", 1)[0]
        self.assertNotIn("import playwright", top_level)
        self.assertIn("from playwright.sync_api import sync_playwright", source)


@unittest.skipUnless(os.environ.get("WCAG_DEMO_URL"),
                     "set WCAG_DEMO_URL to a served site to run the real crawl")
class LiveCrawlTests(unittest.TestCase):
    """Set WCAG_DEMO_URL=http://127.0.0.1:8765/ after `python3
    examples/serve_demo.py`, and AXE_CORE_PATH or --axe-path to a real axe-core."""

    def test_crawl_writes_json_and_html(self):
        out = REPO / "tests" / "_tmp_report"
        axe = os.environ.get("AXE_CORE_PATH", str(REPO / "node_modules/axe-core/axe.min.js"))
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--url", os.environ["WCAG_DEMO_URL"],
             "--axe-path", axe, "--out", str(out)],
            cwd=str(REPO), capture_output=True, text=True, timeout=900,
        )
        try:
            self.assertEqual(proc.returncode, 0, combined(proc))
            self.assertIn("Audited", proc.stdout)
            self.assertIn("Full conformance can be asserted from this audit: no",
                          proc.stdout)
            json_path = out.with_suffix(".json")
            self.assertTrue(json_path.is_file())
            import json

            report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertGreater(report["summary"]["pages_audited"], 0)
            self.assertFalse(report["conformance"]["can_assert_full_conformance"])
            self.assertGreater(report["summary"]["total_violation_instances"], 0)
        finally:
            for suffix in (".json", ".html"):
                path = out.with_suffix(suffix)
                if path.exists():
                    path.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
