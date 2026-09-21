#!/usr/bin/env python3
"""Tests for extra-repo discovery and Mythos gh skip.

    python3 test_scan.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import ghinfo
import scan as scanmod
import repodash


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=path, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return path


class ExtraRepos(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_includes_extra_git_repo(self):
        inside = init_repo(self.root / "alpha")
        outside = init_repo(self.root / "elsewhere" / "mythos-docs")
        data = scanmod.scan_all(str(self.root), extra=[str(outside)])
        names = {r["name"] for r in data["repos"]}
        self.assertIn("alpha", names)
        self.assertIn("mythos-docs", names)
        self.assertEqual(data["extra"], [str(outside)])

    def test_missing_extra_is_skipped(self):
        init_repo(self.root / "alpha")
        data = scanmod.scan_all(
            str(self.root), extra=[str(self.root / "no-such-repo")]
        )
        self.assertEqual([r["name"] for r in data["repos"]], ["alpha"])

    def test_duplicate_extra_under_root_is_not_scanned_twice(self):
        alpha = init_repo(self.root / "alpha")
        data = scanmod.scan_all(str(self.root), extra=[str(alpha)])
        names = [r["name"] for r in data["repos"]]
        self.assertEqual(names.count("alpha"), 1)

    def test_extra_directory_of_repos(self):
        init_repo(self.root / "alpha")
        extra_root = self.root / "home-repos"
        init_repo(extra_root / "mythos")
        init_repo(extra_root / "mythos-docs")
        data = scanmod.scan_all(str(self.root), extra=[str(extra_root)])
        names = {r["name"] for r in data["repos"]}
        self.assertEqual(names, {"alpha", "mythos", "mythos-docs"})

    def test_load_extra_repos_ignores_comments(self):
        cfg = self.root / "extra-repos.txt"
        cfg.write_text(
            "# comment\n\n~/mythos\n~/mythos-docs\n", encoding="utf-8"
        )
        extras = scanmod.load_extra_repos(str(cfg))
        self.assertEqual(
            extras,
            [os.path.expanduser("~/mythos"),
             os.path.expanduser("~/mythos-docs")],
        )


class GhSkip(unittest.TestCase):
    def test_personal_github_repo_is_eligible(self):
        self.assertTrue(ghinfo.gh_eligible({
            "remote_host": "github.com",
            "remote_slug": "sparktron/repo-dashboard",
        }))

    def test_mythos_org_is_not_eligible(self):
        self.assertFalse(ghinfo.gh_eligible({
            "remote_host": "github.com",
            "remote_slug": "mythos-ai/mythos-docs",
        }))
        self.assertFalse(ghinfo.gh_eligible({
            "remote_host": "github.com",
            "remote_slug": "mythosDylan/Experimental",
        }))

    def test_non_github_is_not_eligible(self):
        self.assertFalse(ghinfo.gh_eligible({
            "remote_host": "gitlab.com",
            "remote_slug": "sparktron/x",
        }))


class MergeExtra(unittest.TestCase):
    def test_dedupes_also_against_file(self):
        extras = repodash.merge_extra(["~/mythos"])
        self.assertEqual(extras.count(os.path.expanduser("~/mythos")), 1)
        self.assertIn(os.path.expanduser("~/mythos-docs"), extras)


if __name__ == "__main__":
    unittest.main()
