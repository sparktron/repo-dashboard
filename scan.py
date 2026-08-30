"""Git repo state collection for repodash.

Pure stdlib. Every field is read from git plumbing or the filesystem -- nothing
here is inferred. Fields git cannot answer are omitted, not guessed.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

GIT_TIMEOUT = 15
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox"}

# Branch namespaces coding agents create. Used to tell "I am mid-session on an
# agent branch" apart from ordinary feature work, and to spot buildup.
AGENT_BRANCH_PREFIXES = (
    "ccode/", "claude/", "codex/", "cursor/", "aider/", "devin/", "copilot/",
)

# Untracked paths that agent tooling drops in every repo. They are expected,
# so they must not make 15 repos shout "uncommitted work" in unison.
NOISE_UNTRACKED = (
    ".claude/", ".claude", ".cursor/", ".cursor", ".aider.chat.history.md",
    ".aider.input.history", ".codex/", "PR_BODY.md", ".DS_Store",
)

MAIN_BRANCHES = ("main", "master", "trunk", "develop")


def is_agent_branch(name):
    return bool(name) and name.startswith(AGENT_BRANCH_PREFIXES)


def is_noise_path(path):
    p = path.strip().strip('"')
    return any(p == n or p.startswith(n.rstrip("/") + "/") or p == n.rstrip("/")
               for n in NOISE_UNTRACKED)


def run(args, cwd, timeout=GIT_TIMEOUT):
    """Run a command, returning (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    except OSError as e:
        return 127, "", str(e)


def git(cwd, *args, timeout=GIT_TIMEOUT):
    return run(["git", "--no-optional-locks", *args], cwd, timeout)


# --------------------------------------------------------------------------
# status --porcelain=v2 parsing
# --------------------------------------------------------------------------

def parse_status(out):
    """Parse `git status --porcelain=v2 --branch`.

    Line kinds: '# ' headers, '1'/'2' changed, 'u' unmerged, '?' untracked.
    In '1'/'2' lines the XY field is staged/worktree; '.' means unmodified.
    """
    st = {
        "branch": None, "oid": None, "upstream": None,
        "ahead": 0, "behind": 0,
        "staged": 0, "unstaged": 0, "untracked": 0, "conflicts": 0,
        "changed_files": [], "untracked_paths": [],
    }
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            st["branch"] = line[14:].strip()
        elif line.startswith("# branch.oid "):
            st["oid"] = line[13:].strip()
        elif line.startswith("# branch.upstream "):
            st["upstream"] = line[18:].strip()
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                st["ahead"], st["behind"] = int(m.group(1)), int(m.group(2))
        elif line[:2] in ("1 ", "2 "):
            parts = line.split(" ")
            xy = parts[1]
            if xy[0] != ".":
                st["staged"] += 1
            if len(xy) > 1 and xy[1] != ".":
                st["unstaged"] += 1
            if len(st["changed_files"]) < 12:
                st["changed_files"].append(line.split("\t")[0].split(" ")[-1])
        elif line.startswith("u "):
            st["conflicts"] += 1
        elif line.startswith("? "):
            st["untracked"] += 1
            if len(st["untracked_paths"]) < 25:
                st["untracked_paths"].append(line[2:])
    st["detached"] = st["branch"] == "(detached)"
    return st


# --------------------------------------------------------------------------
# in-progress operations -- the "agent stopped mid-flight" signals
# --------------------------------------------------------------------------

def git_operation(gitdir):
    """Detect an interrupted git operation from marker files in the git dir."""
    def has(*p):
        return os.path.exists(os.path.join(gitdir, *p))

    if has("rebase-merge") or has("rebase-apply"):
        return "rebase"
    if has("MERGE_HEAD"):
        return "merge"
    if has("CHERRY_PICK_HEAD"):
        return "cherry-pick"
    if has("REVERT_HEAD"):
        return "revert"
    if has("BISECT_LOG"):
        return "bisect"
    return None


def stale_lock(gitdir):
    """index.lock left behind means a git process died or is still running."""
    lock = os.path.join(gitdir, "index.lock")
    try:
        age = time.time() - os.path.getmtime(lock)
    except OSError:
        return None
    return {"age_seconds": int(age)}


# --------------------------------------------------------------------------
# remote parsing
# --------------------------------------------------------------------------

def parse_remote(url):
    """Extract (host, owner/name) from an ssh or https remote URL."""
    if not url:
        return None, None
    u = url.strip()
    m = re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/](.+?)(?:\.git)?/?$", u)
    if u.startswith(("http://", "https://")):
        m = re.match(r"^https?://(?:[^@/]+@)?([\w.-]+)/(.+?)(?:\.git)?/?$", u)
    if not m:
        return None, None
    host, slug = m.group(1), m.group(2)
    return host, slug if slug.count("/") == 1 else None


# --------------------------------------------------------------------------
# per-repo scan
# --------------------------------------------------------------------------

def scan_repo(path):
    name = os.path.basename(path.rstrip("/"))
    r = {"name": name, "path": path, "errors": []}

    rc, gitdir, err = git(path, "rev-parse", "--absolute-git-dir")
    if rc != 0:
        r["errors"].append("not a git repo: %s" % (err or "rev-parse failed"))
        return r
    r["git_dir"] = gitdir

    rc, out, err = git(path, "status", "--porcelain=v2", "--branch")
    if rc != 0:
        r["errors"].append("status failed: %s" % err)
        return r
    r.update(parse_status(out))

    noise = [p for p in r["untracked_paths"] if is_noise_path(p)]
    r["untracked_noise"] = len(noise)
    r["untracked_real"] = r["untracked"] - len(noise)
    r["noise_paths"] = noise
    # `dirty` stays literal (any change at all); `dirty_real` is what the
    # attention rules key off, so agent scaffolding never raises an alarm.
    r["dirty"] = bool(r["staged"] or r["unstaged"] or r["untracked"] or r["conflicts"])
    r["dirty_real"] = bool(r["staged"] or r["unstaged"] or r["untracked_real"]
                           or r["conflicts"])
    r["on_agent_branch"] = is_agent_branch(r.get("branch"))
    r["operation"] = git_operation(gitdir)
    r["index_lock"] = stale_lock(gitdir)

    # last commit
    rc, out, _ = git(path, "log", "-1", "--format=%h%x1f%H%x1f%ct%x1f%an%x1f%s")
    if rc == 0 and out:
        f = out.split("\x1f")
        if len(f) == 5:
            r["last_commit"] = {
                "short": f[0], "hash": f[1], "ts": int(f[2]),
                "author": f[3], "subject": f[4],
            }
    else:
        r["last_commit"] = None  # unborn branch / empty repo

    # remotes
    rc, out, _ = git(path, "remote", "get-url", "origin")
    r["remote_url"] = out if rc == 0 and out else None
    r["has_remote"] = r["remote_url"] is not None
    r["remote_host"], r["remote_slug"] = parse_remote(r["remote_url"])

    # every local branch + its tracking state (catches work parked on a
    # side branch that `status` alone would never show)
    rc, out, _ = git(
        path, "for-each-ref", "refs/heads",
        "--format=%(refname:short)%09%(upstream:short)%09%(upstream:track)%09%(committerdate:unix)",
    )
    branches, unpushed = [], []
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            bname, up, track, cdate = parts[0], parts[1], parts[2], parts[3]
            ahead = behind = 0
            if track:
                ma = re.search(r"ahead (\d+)", track)
                mb = re.search(r"behind (\d+)", track)
                ahead = int(ma.group(1)) if ma else 0
                behind = int(mb.group(1)) if mb else 0
            gone = "gone" in track
            b = {
                "name": bname, "upstream": up or None,
                "ahead": ahead, "behind": behind, "gone": gone,
                "ts": int(cdate) if cdate.isdigit() else None,
            }
            branches.append(b)
            if ahead > 0 or (not up and bname not in ("main", "master")):
                unpushed.append(b)
    r["branches"] = branches
    r["branch_count"] = len(branches)
    agent_b = [b for b in branches if is_agent_branch(b["name"])]
    r["agent_branches"] = agent_b
    r["agent_branch_count"] = len(agent_b)
    # An agent branch nobody pushed and nobody is on: abandoned session work.
    r["abandoned_agent_branches"] = [
        b for b in agent_b
        if b["name"] != r.get("branch") and (not b["upstream"] or b["ahead"] > 0)
    ]
    r["unpushed_branches"] = sorted(unpushed, key=lambda b: -b["ahead"])
    # Unpushed work on ordinary branches -- the agent ones are counted above.
    r["other_unpushed_branches"] = [
        b for b in r["unpushed_branches"]
        if b["name"] != r.get("branch") and not is_agent_branch(b["name"])
    ]

    # stashes
    rc, out, _ = git(path, "stash", "list")
    r["stash_count"] = len([l for l in out.splitlines() if l]) if rc == 0 else 0

    # extra worktrees (an agent working in parallel checkouts)
    rc, out, _ = git(path, "worktree", "list", "--porcelain")
    wts = []
    if rc == 0:
        cur = {}
        for line in out.splitlines():
            if line.startswith("worktree "):
                if cur:
                    wts.append(cur)
                cur = {"path": line[9:]}
            elif line.startswith("branch "):
                cur["branch"] = line[7:].replace("refs/heads/", "")
            elif line.strip() == "detached":
                cur["branch"] = "(detached)"
        if cur:
            wts.append(cur)
    r["worktrees"] = [w for w in wts if os.path.realpath(w["path"]) != os.path.realpath(path)]

    # agent-development markers
    def isfile(*p):
        return os.path.isfile(os.path.join(path, *p))

    def isdir(*p):
        return os.path.isdir(os.path.join(path, *p))

    r["markers"] = {
        "claude_md": isfile("CLAUDE.md"),
        "agents_md": isfile("AGENTS.md"),
        "claude_dir": isdir(".claude"),
        "cursor_rules": isdir(".cursorrules") or isfile(".cursorrules") or isdir(".cursor"),
        "mcp_config": isfile(".mcp.json") or isfile("mcp.json"),
        "github_actions": isdir(".github", "workflows"),
    }

    # last time git itself touched this repo (proxy for local agent activity)
    for f in ("index", "HEAD"):
        try:
            r.setdefault("git_touched_ts", 0)
            r["git_touched_ts"] = max(r["git_touched_ts"],
                                      int(os.path.getmtime(os.path.join(gitdir, f))))
        except OSError:
            pass
    if not r.get("git_touched_ts"):
        r["git_touched_ts"] = None

    return r


# --------------------------------------------------------------------------
# classification -- turns raw git facts into an attention level
# --------------------------------------------------------------------------

# Ordered worst-first. The first matching rule wins for the headline status;
# every matching rule is recorded in `flags` so nothing is hidden by ranking.
RULES = [
    ("conflicts",   "critical", lambda r: r.get("conflicts", 0) > 0,
     lambda r: "%d file(s) in merge conflict" % r["conflicts"]),
    ("operation",   "critical", lambda r: r.get("operation"),
     lambda r: "%s in progress" % r["operation"]),
    # A lock younger than LIVE_LOCK_SECONDS is a git process working right now
    # -- that is activity, not damage. Older than that and no process ever
    # cleaned up: git commands in that repo will fail until it is removed.
    ("stale-lock",  "serious",  lambda r: r.get("index_lock")
        and r["index_lock"]["age_seconds"] >= LIVE_LOCK_SECONDS,
     lambda r: "stale index.lock, %s old -- git commands here will fail until it is removed"
               % human_age(r["index_lock"]["age_seconds"])),
    ("live-lock",   "info",     lambda r: r.get("index_lock")
        and r["index_lock"]["age_seconds"] < LIVE_LOCK_SECONDS,
     lambda r: "git operation running right now (lock %s old)"
               % human_age(r["index_lock"]["age_seconds"])),
    ("detached",    "serious",  lambda r: r.get("detached"),
     lambda r: "detached HEAD -- commits here are not on any branch"),
    ("diverged",    "serious",  lambda r: r.get("ahead", 0) > 0 and r.get("behind", 0) > 0,
     lambda r: "diverged from upstream: %d ahead, %d behind" % (r["ahead"], r["behind"])),
    ("unpushed",    "warning",  lambda r: r.get("ahead", 0) > 0,
     lambda r: "%d commit(s) not pushed" % r["ahead"]),
    ("uncommitted", "warning",  lambda r: r.get("dirty_real"),
     lambda r: describe_dirty(r)),
    ("on-agent-branch", "warning", lambda r: r.get("on_agent_branch"),
     lambda r: "checked out on agent branch %s, not %s -- an agent session was "
               "left mid-flight here" % (r.get("branch"), MAIN_BRANCHES[0])),
    ("agent-branch-buildup", "warning",
     lambda r: len(r.get("abandoned_agent_branches") or []) >= 5,
     lambda r: "%d abandoned agent branches with unpushed or untracked commits"
               % len(r["abandoned_agent_branches"])),
    ("agent-scaffolding", "info",
     lambda r: r.get("untracked_noise") and not r.get("dirty_real"),
     lambda r: "only agent scaffolding untracked (%s) -- add to .gitignore to silence"
               % ", ".join(r["noise_paths"][:3])),
    # Agent branches are covered by their own rules; this is for human branches.
    ("side-branch", "warning",  lambda r: bool(r.get("other_unpushed_branches")),
     lambda r: "%d non-agent branch(es) with unpushed work"
               % len(r["other_unpushed_branches"])),
    ("behind",      "info",     lambda r: r.get("behind", 0) > 0,
     lambda r: "%d commit(s) behind upstream" % r["behind"]),
    ("no-upstream", "info",     lambda r: r.get("has_remote") and not r.get("upstream"),
     lambda r: "branch has no upstream -- nothing tracks it"),
    ("no-remote",   "info",     lambda r: not r.get("has_remote"),
     lambda r: "no origin remote -- local only"),
    ("stashed",     "info",     lambda r: r.get("stash_count", 0) > 0,
     lambda r: "%d stash entr(ies)" % r["stash_count"]),
    ("worktrees",   "info",     lambda r: bool(r.get("worktrees")),
     lambda r: "%d extra worktree(s) checked out" % len(r["worktrees"])),
    ("empty",       "info",     lambda r: r.get("last_commit") is None,
     lambda r: "no commits yet"),
]

LIVE_LOCK_SECONDS = 120

SEVERITY_ORDER = {"critical": 0, "serious": 1, "warning": 2, "info": 3, "good": 4}


def describe_dirty(r):
    bits = []
    for k, label in (("staged", "staged"), ("unstaged", "unstaged"),
                     ("untracked_real", "untracked")):
        if r.get(k):
            bits.append("%d %s" % (r[k], label))
    return "uncommitted: " + ", ".join(bits)


def human_age(seconds):
    if seconds is None:
        return "unknown"
    s = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return "%d%s" % (s // size, unit)
    return "%ds" % s


def classify(r):
    if r.get("errors"):
        r["status"] = "critical"
        r["flags"] = [{"id": "error", "level": "critical", "text": r["errors"][0]}]
        r["headline"] = r["errors"][0]
        return r

    flags = []
    for fid, level, test, msg in RULES:
        try:
            if test(r):
                flags.append({"id": fid, "level": level, "text": msg(r)})
        except Exception as e:  # a rule must never take the scan down
            flags.append({"id": fid, "level": "info", "text": "rule error: %s" % e})

    r["flags"] = flags
    actionable = [f for f in flags if f["level"] != "info"]
    if not flags:
        r["status"] = "good"
        r["headline"] = "clean and in sync"
    else:
        r["status"] = min(flags, key=lambda f: SEVERITY_ORDER[f["level"]])["level"]
        r["headline"] = (actionable or flags)[0]["text"]
    r["needs_action"] = bool(actionable)
    return r


# --------------------------------------------------------------------------
# root walk
# --------------------------------------------------------------------------

def discover(root):
    """Return (git_repo_paths, non_git_dirs) for direct children of root."""
    repos, plain = [], []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError as e:
        return [], [{"name": "<root unreadable>", "reason": str(e)}]

    for e in entries:
        if not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
            continue
        if e.name in SKIP_DIRS:
            continue
        gitpath = os.path.join(e.path, ".git")
        if os.path.exists(gitpath):
            repos.append(e.path)
        else:
            plain.append(e.path)
    return repos, plain


def describe_plain(path):
    """A non-git directory: is it a project someone forgot to init?"""
    name = os.path.basename(path)
    d = {"name": name, "path": path}
    try:
        names = os.listdir(path)
    except OSError as e:
        d["error"] = str(e)
        return d

    d["entry_count"] = len(names)
    project_signals = [
        n for n in (
            "package.json", "pyproject.toml", "setup.py", "Cargo.toml",
            "go.mod", "Makefile", "CMakeLists.txt", "requirements.txt",
            "CLAUDE.md", "AGENTS.md", "README.md",
        ) if n in names
    ]
    d["project_signals"] = project_signals
    d["looks_like_project"] = bool(project_signals)
    try:
        d["mtime"] = int(os.path.getmtime(path))
    except OSError:
        d["mtime"] = None
    return d


def scan_all(root, workers=16):
    started = time.time()
    repo_paths, plain_paths = discover(root)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        repos = list(pool.map(scan_repo, repo_paths))
    repos = [classify(r) for r in repos]

    non_git = [describe_plain(p) for p in plain_paths]

    counts = {k: 0 for k in ("critical", "serious", "warning", "info", "good")}
    for r in repos:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "generated_at": int(time.time()),
        "root": root,
        "scan_seconds": round(time.time() - started, 2),
        "repos": sorted(repos, key=lambda r: (SEVERITY_ORDER[r["status"]],
                                              r["name"].lower())),
        "non_git": non_git,
        "summary": {
            "repo_count": len(repos),
            "non_git_count": len(non_git),
            "needs_action": sum(1 for r in repos if r.get("needs_action")),
            "dirty": sum(1 for r in repos if r.get("dirty_real")),
            "on_agent_branch": sum(1 for r in repos if r.get("on_agent_branch")),
            "abandoned_agent_branches": sum(
                len(r.get("abandoned_agent_branches") or []) for r in repos),
            "repos_with_agent_branches": sum(
                1 for r in repos if r.get("agent_branch_count")),
            "unpushed": sum(1 for r in repos if r.get("ahead", 0) > 0),
            "behind": sum(1 for r in repos if r.get("behind", 0) > 0),
            "no_remote": sum(1 for r in repos if not r.get("has_remote")),
            "with_agent_markers": sum(
                1 for r in repos
                if any((r.get("markers") or {}).get(k)
                       for k in ("claude_md", "agents_md", "claude_dir"))
            ),
            "counts": counts,
        },
    }
