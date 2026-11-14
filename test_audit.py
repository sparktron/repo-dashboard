"""Regression cases for the October 2026 repository audit. All remotes are local."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import repodash
import scan
import test_scan


class AuditRepo(unittest.TestCase):
    def setUp(self):
        self.fixture = test_scan.Classification()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.work = self.fixture.work
        self.bare = self.fixture.bare

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.work), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        test_scan.sh(self.work, "commit", "-q", "--allow-empty", "-m", message)

    def prepare_pull(self):
        (self.work / "tracked").write_text("old\n")
        test_scan.sh(self.work, "add", "tracked")
        self.commit("file")
        test_scan.sh(self.work, "push", "-q")
        old = self.git("rev-parse", "HEAD")
        (self.work / "tracked").write_text("new\n")
        test_scan.sh(self.work, "add", "tracked")
        self.commit("remote update")
        new = self.git("rev-parse", "HEAD")
        test_scan.sh(self.work, "push", "-q")
        test_scan.sh(self.work, "reset", "--hard", old)
        self.git("branch", "innocent")
        return old, new

    def test_pull_refuses_checkout_during_fetch(self):
        old, _ = self.prepare_pull()
        r = scan.scan_repo(str(self.work))
        real = subprocess.Popen
        switched = []

        def switch(args, *a, **kw):
            if "fetch" in args or "pull" in args:
                if not switched:
                    switched.append(True)
                    self.git("switch", "-q", "innocent")
            return real(args, *a, **kw)

        with mock.patch.object(subprocess, "Popen", side_effect=switch):
            ok, output = repodash.run_git_action(r, "pull")
        self.assertFalse(ok, output)
        self.assertEqual(self.git("rev-parse", "main"), old)
        self.assertEqual(self.git("rev-parse", "innocent"), old)
        self.assertEqual((self.work / "tracked").read_text(), "old\n")

    def test_pull_blocks_checkout_through_worktree_update(self):
        old, new = self.prepare_pull()
        r = scan.scan_repo(str(self.work))
        real = subprocess.Popen
        attempts = []

        def checkout(args, *a, **kw):
            if "read-tree" in args:
                attempt = subprocess.run(["git", "-C", str(self.work), "switch", "innocent"],
                                         capture_output=True, text=True)
                attempts.append(attempt.returncode)
            return real(args, *a, **kw)

        with mock.patch.object(subprocess, "Popen", side_effect=checkout):
            ok, output = repodash.run_git_action(r, "pull")
        self.assertTrue(ok, output)
        self.assertTrue(attempts)
        self.assertTrue(all(attempts))
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.git("rev-parse", "main"), new)
        self.assertEqual(self.git("rev-parse", "innocent"), old)
        self.assertEqual((self.work / "tracked").read_text(), "new\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.work / ".git/index.lock").exists())
        self.assertFalse((self.work / ".git/HEAD.lock").exists())

    def test_pull_preserves_dirty_files_and_diverged_branches(self):
        old, _ = self.prepare_pull()
        (self.work / "tracked").write_text("local work\n")
        ok, _ = repodash.run_git_action(scan.scan_repo(str(self.work)), "pull")
        self.assertFalse(ok)
        self.assertEqual((self.work / "tracked").read_text(), "local work\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), old)
        self.git("restore", "tracked")
        self.commit("divergence")
        before = self.git("rev-parse", "HEAD")
        ok, _ = repodash.run_git_action(scan.scan_repo(str(self.work)), "pull")
        self.assertFalse(ok)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_pull_aborted_ref_update_restores_worktree(self):
        old, _ = self.prepare_pull()
        with mock.patch("git_actions.RefUpdate.commit", side_effect=RuntimeError("refused")):
            ok, output = repodash.run_git_action(scan.scan_repo(str(self.work)), "pull")
        self.assertFalse(ok, output)
        self.assertEqual(self.git("rev-parse", "HEAD"), old)
        self.assertEqual((self.work / "tracked").read_text(), "old\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.work / ".git/HEAD.lock").exists())

    def test_pull_timeout_after_worktree_update_restores_original_checkout(self):
        old, _ = self.prepare_pull()
        import processes
        run = processes.run
        interrupted = []

        def timed_out(args, *a, **kw):
            result = run(args, *a, **kw)
            if "read-tree" in args and not interrupted:
                interrupted.append(True)
                return 124, "", "timed out after writing the working tree"
            return result

        with mock.patch.object(processes, "run", side_effect=timed_out):
            ok, output = repodash.run_git_action(scan.scan_repo(str(self.work)), "pull")
        self.assertFalse(ok, output)
        self.assertEqual(self.git("rev-parse", "HEAD"), old)
        self.assertEqual((self.work / "tracked").read_text(), "old\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_pull_does_not_overwrite_untracked_files(self):
        old, _ = self.prepare_pull()
        test_scan.sh(self.work, "switch", "-q", "--detach", "origin/main")
        (self.work / "untracked").write_text("remote\n")
        test_scan.sh(self.work, "add", "untracked")
        self.commit("remote file")
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("switch", "-q", "main")
        (self.work / "untracked").write_text("precious local file\n")
        ok, output = repodash.run_git_action(scan.scan_repo(str(self.work)), "pull")
        self.assertFalse(ok, output)
        self.assertEqual(self.git("rev-parse", "HEAD"), old)
        self.assertEqual((self.work / "untracked").read_text(), "precious local file\n")
        self.assertEqual((self.work / "tracked").read_text(), "old\n")

    def test_pull_lost_commit_acknowledgement_keeps_index_consistent(self):
        _, new = self.prepare_pull()
        import git_actions
        commit = git_actions.RefUpdate.commit

        def lost_ack(transaction):
            commit(transaction)
            raise TimeoutError("acknowledgement lost")

        with mock.patch.object(git_actions.RefUpdate, "commit", lost_ack):
            ok, output = repodash.run_git_action(scan.scan_repo(str(self.work)), "pull")
        self.assertTrue(ok, output)
        self.assertEqual(self.git("rev-parse", "HEAD"), new)
        self.assertEqual((self.work / "tracked").read_text(), "new\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_pull_in_linked_worktree_uses_its_own_head_and_index(self):
        old, new = self.prepare_pull()
        linked = self.work.parent / "linked"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked), old)
        test_scan.sh(linked, "branch", "--set-upstream-to", "origin/main")
        ok, output = repodash.run_git_action(scan.scan_repo(str(linked)), "pull")
        self.assertTrue(ok, output)
        self.assertEqual(self.git("rev-parse", "main"), old)
        self.assertEqual(self.git("rev-parse", "linked"), new)
        self.assertEqual((linked / "tracked").read_text(), "new\n")
        status = subprocess.run(["git", "-C", str(linked), "status", "--porcelain"],
                                check=True, text=True, capture_output=True).stdout
        self.assertEqual(status, "")

    def test_parked_deleted_upstreams_preserve_unique_work(self):
        for name in ("feature/parked", "codex/parked"):
            self.git("switch", "-q", "-c", name, "main")
            self.commit(name)
            self.git("push", "-q", "-u", "origin", name)
            self.git("switch", "-q", "main")
            self.git("push", "-q", "origin", ":" + name)
        self.git("fetch", "-q", "--prune")
        r = scan.classify(scan.scan_repo(str(self.work)))
        self.assertTrue(r["needs_action"])
        self.assertEqual({b["name"] for b in r["unpushed_branches"]},
                         {"feature/parked", "codex/parked"})
        self.assertEqual([b["name"] for b in r["abandoned_agent_branches"]], ["codex/parked"])

    def test_parked_deleted_upstream_already_published_elsewhere_is_not_unpushed(self):
        self.git("switch", "-q", "-c", "feature/merged")
        self.commit("merged")
        self.git("push", "-q", "-u", "origin", "HEAD")
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("switch", "-q", "main")
        self.git("push", "-q", "origin", ":feature/merged")
        self.git("fetch", "-q", "--prune")
        r = scan.classify(scan.scan_repo(str(self.work)))
        self.assertFalse(r["unpushed_branches"])
        self.assertFalse(r["needs_action"])

    def test_one_parked_agent_branch_with_unique_work_is_actionable(self):
        self.git("switch", "-q", "-c", "codex/parked")
        self.commit("parked")
        self.git("push", "-q", "-u", "origin", "HEAD")
        self.git("switch", "-q", "main")
        self.git("push", "-q", "origin", ":codex/parked")
        self.git("fetch", "-q", "--prune")
        r = scan.classify(scan.scan_repo(str(self.work)))
        self.assertTrue(r["needs_action"])
        self.assertNotEqual(r["status"], "good")

    def test_query_failures_are_not_healthy_zeroes(self):
        original = scan.git
        for command in ("for-each-ref", "stash", "worktree", "remote", "log"):
            with self.subTest(command=command):
                def fail(path, *args, **kwargs):
                    return (124, "", "timed out") if args[0] == command else original(path, *args, **kwargs)
                with mock.patch.object(scan, "git", side_effect=fail):
                    r = scan.classify(scan.scan_repo(str(self.work)))
                self.assertTrue(r["needs_action"], r)
                self.assertNotEqual(r["status"], "good")
                self.assertFalse(r["complete"])
        with mock.patch.object(scan, "git", return_value=(124, "", "timed out")):
            self.assertIsNone(scan.local_only_count(str(self.work), "HEAD"))

    def test_secret_redaction_at_scan_and_export_boundaries(self):
        secret = "synthetic-audit-secret"
        for remote in (f"https://user:{secret}@example.invalid/org/repo.git?token={secret}",
                       f"ssh://user:{secret}@example.invalid/org/repo.git"):
            with self.subTest(remote=remote):
                self.git("remote", "set-url", "origin", remote)
                r = scan.scan_repo(str(self.work))
                self.assertNotIn(secret, json.dumps(r))
                self.assertEqual(r["remote_slug"], "org/repo")
                html = repodash.render_page({"repos": [dict(r, remote_url=remote)]})
                self.assertNotIn(secret, html)

    def test_git_action_timeout_stops_hook_descendants(self):
        marker = self.work / "child-finished"
        hook = self.work / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\nsleep 0.7\necho survived > '%s'\n" % marker)
        hook.chmod(0o700)
        self.commit("local")
        with mock.patch.object(repodash, "GIT_ACTION_TIMEOUT", 0.15):
            ok, output = repodash.run_git_action(scan.scan_repo(str(self.work)), "push")
        self.assertFalse(ok)
        self.assertIn("timed out", output)
        time.sleep(0.85)
        self.assertFalse(marker.exists(), "pre-push descendant survived the timeout")


class AuditCLI(unittest.TestCase):
    def test_scan_exit_code_is_independent_of_output_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            test_scan.init_repo(root / "unborn")
            for json_flag in ([], ["--json"]):
                for fail_flag in ([], ["--exit-code"]):
                    args = ["--root", str(root), "--no-gh", "scan", *json_flag, *fail_flag]
                    with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = repodash.main(args)
                    # An unborn local repo is informational; make it actionable deterministically.
                    d = scan.scan_all(str(root))
                    d["summary"]["needs_action"] = 1
                    with mock.patch.object(repodash.Store, "refresh", return_value=d), \
                            contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = repodash.main(args)
                    self.assertEqual(rc, 1 if fail_flag else 0)
                    if json_flag:
                        self.assertEqual(json.loads(out.getvalue())["summary"]["needs_action"], 1)

    def test_discovery_failure_is_structured_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(scan.os, "scandir", side_effect=PermissionError("denied")):
            d = scan.scan_all(root)
            self.assertFalse(d["complete"])
            self.assertEqual(d["errors"][0]["path"], root)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = repodash.main(["--root", root, "--no-gh", "scan", "--json", "--exit-code"])
            self.assertNotEqual(code, 0)
            self.assertTrue(json.loads(out.getvalue())["errors"])

    def test_extra_directory_failure_does_not_hide_healthy_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, extra = Path(tmp) / "root", Path(tmp) / "extra"
            root.mkdir()
            extra.mkdir()
            test_scan.init_repo(root / "repo")
            original = os.scandir

            def scandir(path):
                if str(path) == str(extra):
                    raise PermissionError("denied")
                return original(path)

            with mock.patch.object(scan.os, "scandir", side_effect=scandir):
                d = scan.scan_all(str(root), extra=[str(extra)])
            self.assertEqual(d["summary"]["repo_count"], 1)
            self.assertEqual(d["errors"][0]["path"], str(extra))
            self.assertFalse(d["complete"])

    def test_invalid_refresh_interval_is_rejected(self):
        for value in ("0", "-1"):
            with self.subTest(value=value), mock.patch.object(repodash, "cmd_serve") as serve:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                    repodash.main(["serve", "--interval", value])
                self.assertEqual(cm.exception.code, 2)
                serve.assert_not_called()


class AuditReliability(unittest.TestCase):
    def test_json_output_is_parsed_before_presentation_redaction(self):
        import ghinfo
        value = [{"title": 'See https://example.invalid/?value="quoted"'}]
        with mock.patch("processes.run", return_value=(0, json.dumps(value), "")):
            result, error = ghinfo._gh_json(["pr", "list"])
        self.assertIsNone(error)
        self.assertEqual(result, value)

    def test_git_actions_disable_credential_prompts_and_redact_errors(self):
        repo = dict(test_scan.GitActionArgs.BASE, path="/unused")
        with mock.patch.object(repodash, "tracking_config", return_value=("origin", "refs/heads/main")), \
                mock.patch("processes.run", return_value=(1, "", "https://u:secret@example.invalid/o/r")) as run:
            ok, output = repodash.run_git_action(repo, "push")
        self.assertFalse(ok)
        self.assertNotIn("secret", output)
        env = run.call_args.args[3]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "/bin/false")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])

    def test_noninteractive_git_preserves_custom_ssh_configuration(self):
        repo = dict(test_scan.GitActionArgs.BASE, path="/unused")
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": ""}), \
                mock.patch.object(repodash, "tracking_config", return_value=("origin", "refs/heads/main")), \
                mock.patch.object(scan, "git", return_value=(0, "ssh -i /keys/project", "")), \
                mock.patch("processes.run", return_value=(0, "done", "")) as run:
            ok, _ = repodash.run_git_action(repo, "push")
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[3]["GIT_SSH_COMMAND"],
                         "ssh -i /keys/project -oBatchMode=yes")

    def test_concurrent_exports_publish_complete_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            barrier = threading.Barrier(2)
            replace = os.replace
            errors = []
            documents = [json.dumps({"writer": n, "value": "x" * 10000}) for n in range(2)]

            def synchronized_replace(source, target):
                barrier.wait(timeout=5)
                replace(source, target)

            def writer(doc):
                try:
                    repodash.atomic_write(str(path), doc)
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(repodash.os, "replace", side_effect=synchronized_replace):
                threads = [threading.Thread(target=writer, args=(doc,)) for doc in documents]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
            self.assertIn(path.read_text(), documents)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_forced_refreshes_coalesce_and_do_not_create_worker_pools(self):
        store = repodash.Store("/tmp", use_gh=False)
        started, release = threading.Event(), threading.Event()
        store.data = {"repos": [], "cached": True}

        def slow(*args, **kwargs):
            started.set()
            release.wait(5)
            return {"repos": [], "cached": False}

        with mock.patch.object(scan, "scan_all", side_effect=slow) as collect:
            thread = threading.Thread(target=store.refresh)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                for _ in range(10):
                    self.assertTrue(store.refresh(force_gh=True, coalesce=True)["cached"])
                self.assertEqual(collect.call_count, 1)
            finally:
                release.set()
                thread.join(5)

    def test_scanner_timeout_stops_background_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child"
            child = "import time; from pathlib import Path; time.sleep(.4); Path(%r).touch()" % str(marker)
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(10)" % child
            rc, _, err = scan.run([sys.executable, "-c", parent], tmp, timeout=0.1)
            self.assertEqual(rc, 124)
            self.assertIn("timed out", err)
            time.sleep(0.5)
            self.assertFalse(marker.exists())

    def test_cached_ci_for_other_commit_is_never_green(self):
        import ghinfo
        r = {"last_commit": {"hash": "local"}}
        entry = {"ci": {"head_sha": "old", "status": "completed", "conclusion": "success"}}
        ghinfo.attach(r, entry)
        self.assertFalse(r["ci_matches_head"])
        self.assertEqual(r["ci_level"], "info")
        entry["ci"]["head_sha"] = "local"
        ghinfo.attach(r, entry)
        self.assertTrue(r["ci_matches_head"])
        self.assertEqual(r["ci_level"], "good")


if __name__ == "__main__":
    unittest.main()
