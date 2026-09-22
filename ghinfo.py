"""Optional GitHub enrichment via the `gh` CLI.

Strictly opt-in and strictly degradable: if `gh` is missing, unauthenticated,
rate-limited, or slow, the dashboard loses this section and nothing else.
Results are cached on disk so a page refresh never re-hits the API.

No credentials are read, stored, or logged here -- authentication is entirely
delegated to `gh`, which holds its own token in its own config.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

from scan import run

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "repodash"
)
CACHE_PATH = os.path.join(CACHE_DIR, "gh.json")
DEFAULT_TTL = 300          # seconds a cached result stays fresh
GH_TIMEOUT = 20            # per gh invocation
MAX_WORKERS = 6            # keep well under secondary rate limits

# gh on this machine is a personal-account login. Do not call it against
# Mythos org remotes (ai-hub AGENTS.md rule 13). GitHub owner names are
# case-insensitive, so compare casefolded.
MYTHOS_GH_ORGS = {o.casefold() for o in ("mythos-ai", "mythosDylan")}


def gh_status():
    """What we can actually do right now, with a human-readable reason."""
    path = shutil.which("gh")
    if not path:
        return {
            "available": False,
            "reason": "gh CLI not installed. Install it and run `gh auth login` "
                      "to light up PR and CI columns.",
        }
    rc, out, err = run(["gh", "auth", "status"], cwd=None, timeout=GH_TIMEOUT)
    if rc != 0:
        return {
            "available": False,
            "path": path,
            "reason": "gh is installed but not authenticated. Run `gh auth login`.",
        }
    return {"available": True, "path": path}


def _gh_json(args, timeout=GH_TIMEOUT):
    rc, out, err = run(["gh", *args], cwd=None, timeout=timeout)
    if rc != 0:
        return None, (err.splitlines()[0] if err else "gh exited %d" % rc)
    if not out:
        return [], None
    try:
        return json.loads(out), None
    except json.JSONDecodeError as e:
        return None, "unparseable gh output: %s" % e


def gh_eligible(repo):
    """Whether this repo may be queried with the personal `gh` login."""
    if repo.get("remote_host") != "github.com":
        return False
    slug = repo.get("remote_slug") or ""
    if not slug or "/" not in slug:
        return False
    owner = slug.split("/", 1)[0]
    return owner.casefold() not in MYTHOS_GH_ORGS


def fetch_repo(slug, branch):
    """Open PRs and the most recent CI run for one GitHub repo."""
    d = {"slug": slug, "fetched_at": int(time.time()), "errors": []}

    prs, err = _gh_json([
        "pr", "list", "--repo", slug, "--state", "open", "--limit", "20",
        "--json", "number,title,headRefName,isDraft,url,updatedAt,reviewDecision",
    ])
    if err:
        d["errors"].append("prs: %s" % err)
    else:
        d["prs"] = [{
            "number": p.get("number"),
            "title": p.get("title"),
            "branch": p.get("headRefName"),
            "draft": bool(p.get("isDraft")),
            "url": p.get("url"),
            "updated_at": p.get("updatedAt"),
            "review": p.get("reviewDecision") or None,
        } for p in (prs or [])]
        d["pr_count"] = len(d["prs"])

    run_args = [
        "run", "list", "--repo", slug, "--limit", "1",
        "--json", "status,conclusion,displayTitle,headBranch,url,createdAt,workflowName",
    ]
    if branch:
        run_args += ["--branch", branch]
    runs, err = _gh_json(run_args)
    if err:
        # A repo with Actions disabled errors here; that is not a failure state.
        if "no runs" not in err.lower():
            d["errors"].append("ci: %s" % err)
    elif runs:
        r = runs[0]
        d["ci"] = {
            "status": r.get("status"),            # queued|in_progress|completed
            "conclusion": r.get("conclusion"),    # success|failure|cancelled|...
            "title": r.get("displayTitle"),
            "branch": r.get("headBranch"),
            "workflow": r.get("workflowName"),
            "url": r.get("url"),
            "created_at": r.get("createdAt"),
        }
    return d


def ci_level(ci):
    """Map a CI result onto the dashboard's status vocabulary."""
    if not ci:
        return None
    if ci.get("status") in ("queued", "in_progress", "waiting", "pending"):
        return "info"
    c = (ci.get("conclusion") or "").lower()
    if c == "success":
        return "good"
    if c in ("failure", "timed_out", "startup_failure"):
        return "critical"
    if c in ("cancelled", "action_required", "stale", "neutral", "skipped"):
        return "warning"
    return "info"


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # cache is an optimisation, never a requirement


def enrich(repos, ttl=DEFAULT_TTL, force=False):
    """Attach a `gh` block to each repo with a github.com remote.

    Returns the gh availability dict. Mutates `repos` in place. Repos whose
    cached entry is still within `ttl` are served from cache without a call.
    """
    status = gh_status()
    cache = load_cache()

    skipped = 0
    targets = []
    for r in repos:
        if gh_eligible(r):
            targets.append(r)
        elif r.get("remote_host") == "github.com" and r.get("remote_slug"):
            r["gh"] = {
                "skipped": True,
                "reason": "Mythos org remote; personal gh login is not used here",
            }
            skipped += 1
    status["candidate_repos"] = len(targets)
    status["skipped_mythos"] = skipped

    if not status["available"]:
        # Serve stale cache if we have one -- better than an empty column.
        for r in targets:
            hit = cache.get(r["remote_slug"])
            if hit:
                r["gh"] = dict(hit, stale=True)
                r["ci_level"] = ci_level(hit.get("ci"))
        return status

    now = time.time()
    todo, served = [], 0
    for r in targets:
        hit = cache.get(r["remote_slug"])
        if hit and not force and now - hit.get("fetched_at", 0) < ttl:
            r["gh"] = hit
            r["ci_level"] = ci_level(hit.get("ci"))
            served += 1
        else:
            todo.append(r)

    if todo:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            results = list(pool.map(
                lambda r: fetch_repo(r["remote_slug"], r.get("branch")), todo
            ))
        for r, res in zip(todo, results):
            r["gh"] = res
            r["ci_level"] = ci_level(res.get("ci"))
            cache[r["remote_slug"]] = res
        save_cache(cache)

    status["from_cache"] = served
    status["fetched"] = len(todo)
    return status
