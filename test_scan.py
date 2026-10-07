#!/usr/bin/env python3
"""Tests for extra-repo discovery, Mythos gh skip, and the pull/push endpoint.

    python3 test_scan.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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

    def test_mythos_org_check_ignores_case(self):
        for slug in ("MYTHOS-AI/repo", "mythosdylan/repo", "MythosDylan/repo"):
            self.assertFalse(ghinfo.gh_eligible({
                "remote_host": "github.com", "remote_slug": slug,
            }), slug)

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


def sh(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class GitActionArgs(unittest.TestCase):
    BASE = {"branch": "main", "detached": False, "operation": None,
            "has_remote": True, "upstream": "origin/main"}

    def args(self, action, **over):
        return repodash.git_action_args(dict(self.BASE, **over), action)

    def test_pull_is_fast_forward_only(self):
        self.assertEqual(self.args("pull"), (["pull", "--ff-only"], None))

    def test_push_never_forces(self):
        self.assertEqual(self.args("push"), (["push"], None))

    def test_push_without_upstream_publishes_to_origin(self):
        self.assertEqual(self.args("push", upstream=None),
                         (["push", "-u", "origin", "main"], None))

    def test_refusals(self):
        self.assertIsNone(self.args("pull", upstream=None)[0])
        self.assertIsNone(self.args("push", detached=True)[0])
        self.assertIsNone(self.args("pull", operation="rebase")[0])
        self.assertIsNone(self.args("push", upstream=None, has_remote=False)[0])
        self.assertIsNone(self.args("fetch")[0])


class GitEndpoint(unittest.TestCase):
    """A real pull and push through the HTTP endpoint, against a bare remote."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        t = Path(self.temp.name)
        self.root = t / "root"
        self.root.mkdir()
        bare = t / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        seed = t / "seed"
        subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True,
                       stderr=subprocess.DEVNULL)
        sh(seed, "checkout", "-q", "-b", "main")
        sh(seed, "commit", "-q", "--allow-empty", "-m", "one")
        sh(seed, "push", "-q", "-u", "origin", "main")
        self.seed = seed
        self.work = self.root / "work"
        subprocess.run(["git", "clone", "-q", str(bare), str(self.work)], check=True)

        self.store = repodash.Store(str(self.root), use_gh=False)
        self.store.refresh()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), repodash.make_handler(self.store))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/api/git" % self.httpd.server_port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.temp.cleanup()

    def post(self, body, headers=None):
        h = {"Content-Type": "application/json", "X-Repodash": "1"}
        h.update(headers or {})
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def head(self, path):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_pull_fast_forwards(self):
        sh(self.seed, "commit", "-q", "--allow-empty", "-m", "two")
        sh(self.seed, "push", "-q")
        code, j = self.post({"path": str(self.work), "action": "pull"})
        self.assertEqual(code, 200)
        self.assertTrue(j["ok"], j["output"])
        self.assertEqual(self.head(self.work), self.head(self.seed))

    def test_push_sends_local_commit(self):
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "local")
        self.store.refresh()
        code, j = self.post({"path": str(self.work), "action": "push"})
        self.assertEqual(code, 200)
        self.assertTrue(j["ok"], j["output"])
        repo = next(r for r in j["data"]["repos"] if r["path"] == str(self.work))
        self.assertEqual(repo["ahead"], 0)

    def test_untracked_path_is_refused(self):
        code, _ = self.post({"path": str(self.seed), "action": "pull"})
        self.assertEqual(code, 404)

    def test_cross_site_requests_are_refused(self):
        body = {"path": str(self.work), "action": "pull"}
        self.assertEqual(self.post(body, {"X-Repodash": ""})[0], 403)
        self.assertEqual(self.post(body, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post(body, {"Origin": "http://127.0.0.1:1"})[0], 200)


if __name__ == "__main__":
    unittest.main()
