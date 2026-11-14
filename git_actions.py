"""Fast-forward a confirmed checkout under Git's own index and ref locks.

Git's prepared update-ref transaction locks HEAD and its resolved branch.
The normal index lock blocks checkout/commit while a private index is used
to update the working tree. No branch mutation depends on a stale scan alone.
"""
from contextlib import ExitStack
import os
import select
import shutil
import subprocess
import tempfile
import time
import uuid

import processes


class RefUpdate:
    def __init__(self, path, old, new, env, deadline):
        self.deadline = deadline
        self.errors = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(
                ["git", "update-ref", "-m", "repodash: fast-forward pull", "--stdin"],
                cwd=path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.errors, start_new_session=True, bufsize=0)
        except OSError:
            self.errors.close()
            raise
        try:
            self.command("start", "start: ok")
            self.command("update HEAD %s %s\nprepare" % (new, old), "prepare: ok")
        except BaseException:
            self.close()
            raise

    def command(self, command, expected):
        try:
            self.proc.stdin.write((command + "\n").encode())
            self.proc.stdin.flush()
            response = b""
            while not response.endswith(b"\n"):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.proc.stdout], [], [], remaining)[0]:
                    raise TimeoutError("pull timed out waiting for Git's ref transaction")
                b = os.read(self.proc.stdout.fileno(), 1)
                if not b:
                    break
                response += b
            if response.decode().strip() != expected:
                self.errors.seek(0)
                raise RuntimeError(self.errors.read().decode(errors="replace").strip()
                                   or "Git refused the ref transaction")
        except BrokenPipeError as exc:
            raise RuntimeError("Git refused the ref transaction") from exc

    def commit(self):
        self.command("commit", "commit: ok")

    def close(self):
        if self.proc.poll() is None:
            # EOF aborts a prepared transaction without publishing its refs.
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                processes.stop_process_group(self.proc)
        for stream in (self.proc.stdin, self.proc.stdout, self.errors):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fast_forward(repo, remote, merge, timeout, env):
    """Fetch an exact source and apply it only to the confirmed clean checkout."""
    path, old = repo["path"], repo.get("oid")
    if not old or old == "(initial)":
        return False, "commit on this branch before pulling"
    deadline = time.monotonic() + timeout
    fetch_ref = "refs/repodash/" + uuid.uuid4().hex

    def git(*args, command_env=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("pull timed out")
        rc, out, err = processes.run(["git", *args], path, remaining, command_env or env)
        if rc:
            raise RuntimeError(err or out or "git exited %d" % rc)
        return out

    try:
        git("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
            remote, "%s:%s" % (merge, fetch_ref))
        new = git("rev-parse", "--verify", fetch_ref + "^{commit}")
        gitdir = git("rev-parse", "--absolute-git-dir")
        index = os.path.join(gitdir, "index")
        with ExitStack() as stack:
            # Use O_EXCL so an existing Git operation is never disturbed.
            fd = os.open(index + ".lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            stack.callback(os.unlink, index + ".lock")
            fd, alternate = tempfile.mkstemp(prefix="repodash-index-", dir=gitdir)
            os.close(fd)
            stack.callback(lambda: os.path.exists(alternate) and os.unlink(alternate))
            shutil.copyfile(index, alternate)
            private_env = dict(env, GIT_INDEX_FILE=alternate)
            with RefUpdate(path, old, new, env, deadline) as transaction:
                # prepare holds HEAD and its resolved ref, even if both branches
                # happened to have the same OID before a concurrent checkout.
                if git("symbolic-ref", "--quiet", "HEAD") != "refs/heads/" + repo["branch"]:
                    return False, "branch changed while fetching -- nothing was run"
                if git("config", "--get", "branch.%s.remote" % repo["branch"]) != remote \
                        or git("config", "--get", "branch.%s.merge" % repo["branch"]) != merge:
                    return False, "upstream changed while fetching -- nothing was run"
                # Treat local-ahead as already up to date, as git pull does.
                rc, _, _ = processes.run(["git", "merge-base", "--is-ancestor", new, old],
                                         path, max(0.001, deadline - time.monotonic()), env)
                if rc == 0:
                    return True, "Already up to date."
                if rc != 1:
                    raise RuntimeError("could not determine fast-forward ancestry")
                git("merge-base", "--is-ancestor", old, new)
                if git("status", "--porcelain", "--untracked-files=no", command_env=private_env):
                    return False, "uncommitted tracked changes -- commit or stash before pulling"
                try:
                    git("read-tree", "-m", "-u", old, new, command_env=private_env)
                    transaction.commit()
                except BaseException as exc:
                    # A lost acknowledgement can follow a successful commit.
                    # Reap/abort first, then inspect the *named* ref before
                    # deciding whether recovery should restore old or new.
                    transaction.close()
                    rc, tip, _ = processes.run(
                        ["git", "rev-parse", "refs/heads/" + repo["branch"]], path, 5, env)
                    if rc == 0 and tip == new:
                        os.replace(alternate, index)
                        if isinstance(exc, (RuntimeError, TimeoutError)):
                            return True, "Fast-forwarded %s (verified after lost acknowledgement)" % repo["branch"]
                    elif rc == 0 and tip == old:
                        restored, _, _ = processes.run(
                            ["git", "read-tree", "-m", "-u", new, old], path, 5, private_env)
                        if restored:
                            raise RuntimeError("pull failed; inspect the working tree before retrying")
                    else:
                        raise RuntimeError("pull outcome could not be verified; inspect the branch before retrying")
                    raise
                os.replace(alternate, index)
        return True, "Fast-forwarded %s from %s to %s" % (repo["branch"], old[:12], new[:12])
    except (OSError, RuntimeError, TimeoutError) as exc:
        return False, str(exc)
    finally:
        processes.run(["git", "update-ref", "-d", fetch_ref], path, 5, env)
