#!/usr/bin/env python3
"""Tests for scanning, classification, gh caching, and the HTTP server.

    python3 test_scan.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
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
            "has_remote": True, "upstream": "origin/main", "ahead": 1,
            "branches": [{"name": "main", "gone": False}]}

    def args(self, action, remote="origin", merge="refs/heads/main", **over):
        return repodash.git_action_args(dict(self.BASE, **over), action, remote, merge)

    def test_pull_is_pinned_and_fast_forward_only(self):
        self.assertEqual(self.args("pull"), (
            ["pull", "--ff-only", "--no-rebase", "origin", "refs/heads/main"], None))

    def test_push_names_remote_and_a_non_forcing_refspec(self):
        args, _ = self.args("push")
        self.assertEqual(args, ["push", "--no-follow-tags", "origin",
                                "refs/heads/main:refs/heads/main"])
        self.assertFalse(any(a.startswith("+") or a.startswith("--force") for a in args))

    def test_push_targets_the_tracked_branch_name(self):
        args, _ = self.args("push", merge="refs/heads/trunk", upstream="up/trunk", remote="up")
        self.assertEqual(args[-2:], ["up", "refs/heads/main:refs/heads/trunk"])

    def test_push_without_upstream_publishes_to_origin(self):
        self.assertEqual(self.args("push", remote=None, merge=None, upstream=None), (
            ["push", "--no-follow-tags", "-u", "origin",
             "refs/heads/main:refs/heads/main"], None))

    def test_gone_upstream_blocks_pull_but_allows_push(self):
        gone = [{"name": "main", "gone": True}]
        self.assertIsNone(self.args("pull", branches=gone)[0])
        self.assertIsNotNone(self.args("push", branches=gone, ahead=0)[0])

    def test_refusals(self):
        self.assertIsNone(self.args("pull", upstream=None)[0])
        self.assertIsNone(self.args("push", detached=True)[0])
        self.assertIsNone(self.args("pull", operation="rebase")[0])
        self.assertIsNone(self.args("push", remote=None, merge=None,
                                    upstream=None, has_remote=False)[0])
        self.assertIsNone(self.args("push", ahead=0)[0])
        # upstream is a local branch (branch.<b>.remote = ".")
        self.assertIsNone(self.args("push", remote=None, merge=None)[0])
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
        body = dict({"branch": "main", "upstream": "origin/main"}, **body)
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

    def test_push_ignores_push_config_that_would_widen_it(self):
        # Without an explicit refspec these would push every branch, forced.
        sh(self.work, "config", "remote.origin.push", "+refs/heads/*:refs/heads/*")
        sh(self.work, "config", "push.default", "matching")
        sh(self.work, "branch", "side")
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "local")
        self.store.refresh()
        code, j = self.post({"path": str(self.work), "action": "push"})
        self.assertTrue(j["ok"], j["output"])
        heads = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=self.work,
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("refs/heads/side", heads)

    def test_branch_switched_since_scan_is_refused(self):
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "local")
        self.store.refresh()                      # the page saw main, ahead 1
        sh(self.work, "checkout", "-q", "-b", "other")
        before = self.head(self.seed)
        code, j = self.post({"path": str(self.work), "action": "push"})
        self.assertFalse(j["ok"])
        self.assertIn("now on other", j["output"])
        sh(self.seed, "fetch", "-q")
        self.assertEqual(self.head(self.seed), before)

    def test_push_recreates_a_deleted_upstream(self):
        sh(self.seed, "push", "-q", "origin", "HEAD:refs/heads/feat")
        sh(self.work, "fetch", "-q")
        sh(self.work, "checkout", "-q", "-b", "feat", "--track", "origin/feat")
        sh(self.seed, "push", "-q", "origin", ":refs/heads/feat")
        sh(self.work, "fetch", "-q", "--prune")
        self.store.refresh()
        repo = self.store.find(str(self.work))
        self.assertFalse(repodash.git_action_args(repo, "pull", "origin", "refs/heads/feat")[0])
        code, j = self.post({"path": str(self.work), "action": "push", "branch": "feat",
                             "upstream": "origin/feat"})
        self.assertTrue(j["ok"], j["output"])

    def test_upstream_changed_since_confirmation_is_refused(self):
        sh(self.seed, "push", "-q", "origin", "HEAD:refs/heads/other")
        sh(self.work, "fetch", "-q")
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "local")
        self.store.refresh()                      # the user confirmed origin/main
        sh(self.work, "branch", "-q", "--set-upstream-to", "origin/other")
        before = subprocess.run(["git", "ls-remote", "origin"], cwd=self.work,
                                capture_output=True, text=True, check=True).stdout
        code, j = self.post({"path": str(self.work), "action": "push"})
        self.assertFalse(j["ok"])
        self.assertIn("now tracks origin/other", j["output"])
        after = subprocess.run(["git", "ls-remote", "origin"], cwd=self.work,
                               capture_output=True, text=True, check=True).stdout
        self.assertEqual(before, after)

    def test_request_without_upstream_is_rejected(self):
        req = urllib.request.Request(
            self.url, method="POST",
            data=json.dumps({"path": str(self.work), "action": "pull",
                             "branch": "main"}).encode(),
            headers={"Content-Type": "application/json", "X-Repodash": "1"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_untracked_path_is_refused(self):
        code, _ = self.post({"path": str(self.seed), "action": "pull"})
        self.assertEqual(code, 404)

    def test_cross_site_requests_are_refused(self):
        body = {"path": str(self.work), "action": "pull"}
        self.assertEqual(self.post(body, {"X-Repodash": ""})[0], 403)
        self.assertEqual(self.post(body, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post(body, {"Origin": "http://127.0.0.1:1"})[0], 200)


class Classification(unittest.TestCase):
    """Repos the dashboard must never call clean."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        t = Path(self.temp.name)
        self.bare = t / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)],
                       check=True)
        self.work = t / "work"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.work)],
                       check=True, stderr=subprocess.DEVNULL)
        sh(self.work, "checkout", "-q", "-b", "main")
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "one")
        sh(self.work, "push", "-q", "-u", "origin", "main")

    def tearDown(self):
        self.temp.cleanup()

    def scan(self):
        return scanmod.classify(scanmod.scan_repo(str(self.work)))

    def ids(self, r):
        return [f["id"] for f in r["flags"]]

    def test_deleted_upstream_with_local_commits_needs_action(self):
        sh(self.work, "checkout", "-q", "-b", "feat")
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "work")
        sh(self.work, "push", "-q", "-u", "origin", "feat")
        sh(self.work, "push", "-q", "origin", ":feat")
        sh(self.work, "fetch", "-q", "--prune")
        r = self.scan()
        self.assertTrue(r["upstream_gone"])
        self.assertEqual(r["local_only_commits"], 1)
        self.assertIn("local-only", self.ids(r))
        self.assertTrue(r["needs_action"])
        self.assertNotEqual(r["status"], "good")

    def test_deleted_upstream_already_on_another_branch_is_info(self):
        sh(self.work, "checkout", "-q", "-b", "feat")
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "work")
        sh(self.work, "push", "-q", "-u", "origin", "feat")
        sh(self.work, "push", "-q", "origin", "feat:main")   # merged
        sh(self.work, "push", "-q", "origin", ":feat")
        sh(self.work, "fetch", "-q", "--prune")
        r = self.scan()
        self.assertEqual(r["local_only_commits"], 0)
        self.assertIn("upstream-gone", self.ids(r))
        self.assertFalse(r["needs_action"])

    def test_never_pushed_branch_with_commits_needs_action(self):
        sh(self.work, "checkout", "-q", "-b", "local")
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "work")
        r = self.scan()
        self.assertEqual(r["local_only_commits"], 1)
        self.assertIn("local-only", self.ids(r))
        self.assertNotIn("no-upstream", self.ids(r))
        self.assertTrue(r["needs_action"])

    def test_never_pushed_branch_without_new_commits_stays_info(self):
        sh(self.work, "checkout", "-q", "-b", "local")
        r = self.scan()
        self.assertEqual(r["local_only_commits"], 0)
        self.assertIn("no-upstream", self.ids(r))
        self.assertFalse(r["needs_action"])

    def test_idle_main_named_branch_with_local_commits_is_flagged(self):
        sh(self.work, "branch", "trunk")              # copy of main: exempt
        sh(self.work, "checkout", "-q", "-b", "develop")
        sh(self.work, "commit", "-q", "--allow-empty", "-m", "work")
        sh(self.work, "checkout", "-q", "main")
        r = self.scan()
        self.assertEqual([b["name"] for b in r["other_unpushed_branches"]], ["develop"])
        self.assertIn("side-branch", self.ids(r))
        self.assertTrue(r["needs_action"])

    def test_unreadable_repo_needs_action(self):
        r = scanmod.classify({"name": "x", "path": "/x", "errors": ["status failed: boom"]})
        self.assertEqual(r["status"], "critical")
        self.assertTrue(r["needs_action"])

    def test_conflict_only_dirty_text_is_not_empty(self):
        self.assertEqual(scanmod.describe_dirty({"conflicts": 2}),
                         "uncommitted: 2 conflicted")


class ParseStatus(unittest.TestCase):
    def test_changed_paths_keep_their_spaces(self):
        st = scanmod.parse_status(
            "1 A. N... 000000 100644 100644 0000 abcd my file.txt\n"
            "2 R. N... 100644 100644 100644 abcd abcd R100 new name.txt\told name.txt\n")
        self.assertEqual(st["changed_files"], ["my file.txt", "new name.txt"])

    def test_noise_is_counted_past_the_sample_cap(self):
        lines = ["? f%02d" % i for i in range(30)] + ["? PR_BODY.md"]
        st = scanmod.parse_status("\n".join(sorted(lines)) + "\n")
        self.assertEqual(st["untracked"], 31)
        self.assertEqual(st["untracked_noise"], 1)


class HostCheck(unittest.TestCase):
    def test_host_hostname(self):
        h = repodash.host_hostname
        self.assertEqual(h("localhost:8787"), "localhost")
        self.assertEqual(h("[::1]:8787"), "::1")
        self.assertEqual(h("[::1]"), "::1")
        self.assertEqual(h("LocalHost"), "localhost")
        self.assertEqual(h(""), "")
        self.assertIsNone(h("[::1"))

    def test_specific_bind_address_is_allowed(self):
        names = repodash.allowed_hostnames("192.168.1.5", ["box.tail.net"])
        self.assertTrue({"192.168.1.5", "box.tail.net", "127.0.0.1"} <= names)
        self.assertEqual(repodash.allowed_hostnames("127.0.0.1"),
                         set(repodash.ALLOWED_HOSTNAMES) | {"127.0.0.1"})

    def get(self, allowed, host):
        store = repodash.Store(tempfile.gettempdir(), use_gh=False)
        store.data = {"repos": []}
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    repodash.make_handler(store, allowed))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/healthz" % httpd.server_port,
                headers={"Host": host})
            try:
                with urllib.request.urlopen(req) as res:
                    return res.status
            except urllib.error.HTTPError as e:
                return e.code
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_non_loopback_bind_is_reachable_by_its_address(self):
        allowed = repodash.allowed_hostnames("192.168.1.5")
        self.assertEqual(self.get(allowed, "192.168.1.5:8787"), 200)
        self.assertEqual(self.get(allowed, "[::1]"), 200)
        self.assertEqual(self.get(allowed, "evil.example:8787"), 403)

    def test_writes_are_refused_from_other_machines(self):
        H = repodash.make_handler(None)
        h = H.__new__(H)
        for addr, local in (("127.0.0.1", True), ("::1", True),
                            ("::ffff:127.0.0.1", True), ("192.168.1.9", False)):
            h.client_address = (addr, 1)
            self.assertEqual(h._client_is_local(), local, addr)


class GhCache(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(ghinfo, k)
                      for k in ("gh_status", "fetch_repo", "load_cache", "save_cache")}
        self.fetched = []
        ghinfo.gh_status = lambda: {"available": True}
        ghinfo.load_cache = lambda: dict(self.cache)
        ghinfo.save_cache = lambda c: None

        def fetch(slug, branch):
            self.fetched.append((slug, branch))
            return {"slug": slug, "fetched_at": int(time.time()), "errors": [],
                    "ci": {"status": "completed", "conclusion": "success",
                           "branch": branch}}
        ghinfo.fetch_repo = fetch

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ghinfo, k, v)

    def repo(self, branch, **over):
        return dict({"remote_host": "github.com", "remote_slug": "sparktron/x",
                     "branch": branch, "detached": False}, **over)

    def test_cached_ci_is_not_served_to_another_branch(self):
        self.cache = {"sparktron/x#main": {"fetched_at": int(time.time()),
                                           "ci": {"branch": "main"}}}
        on_main, on_feat = self.repo("main"), self.repo("feat")
        ghinfo.enrich([on_main, on_feat])
        self.assertEqual(on_main["gh"]["ci"]["branch"], "main")
        self.assertEqual(on_feat["gh"]["ci"]["branch"], "feat")
        self.assertEqual(self.fetched, [("sparktron/x", "feat")])

    def test_same_key_is_fetched_once_and_detached_asks_for_no_branch(self):
        self.cache = {}
        a, b = self.repo("main"), self.repo("main")
        d = self.repo("(detached)", detached=True)
        ghinfo.enrich([a, b, d])
        self.assertEqual(sorted(self.fetched, key=str),
                         sorted([("sparktron/x", "main"), ("sparktron/x", None)], key=str))
        self.assertIs(a["gh"], b["gh"])


class StoreOrdering(unittest.TestCase):
    def test_older_scan_finishing_last_does_not_win(self):
        store = repodash.Store(tempfile.gettempdir(), use_gh=False)
        first_started, release_first = threading.Event(), threading.Event()
        calls = []

        def fake_scan_all(root, extra=None):
            n = len(calls)
            calls.append(n)
            if n == 0:
                first_started.set()
                release_first.wait(5)
            return {"n": n, "repos": []}

        saved = scanmod.scan_all
        scanmod.scan_all = fake_scan_all
        try:
            t = threading.Thread(target=store.refresh)
            t.start()
            first_started.wait(5)
            store.refresh()             # newer scan lands first
            release_first.set()
            t.join(5)
        finally:
            scanmod.scan_all = saved
        self.assertEqual(store.data["n"], 1)


if __name__ == "__main__":
    unittest.main()
