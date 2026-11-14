# repodash

A live status dashboard for every git repo under `~/repos`, plus extra trees
listed in `extra-repos.txt` (none by default; company checkouts are
deliberately not listed there, see that file). Built to answer one question quickly: **where did an agent
leave something unfinished?**

Stdlib Python only — no pip install, no dependencies. The supported runtime is
POSIX (Linux/macOS), Python 3.10+, and Git 2.34+. Scanning is read-only.
The only writes are the **Pull** / **Push** buttons on each row of the live
page, and they run only when you click. Both name the remote and the exact
branch, so `remote.<name>.push`, `push.default` and push remotes can't widen
them: Pull fetches the exact upstream, then fast-forwards the confirmed
checkout under Git's index and prepared reference-transaction locks. It
refuses divergent history, changed branches/commits, and tracked local changes;
commit or stash tracked changes before pulling. Push is
`git push <remote> refs/heads/<branch>:<upstream ref>` (never forced), or
`git push -u origin <branch>` for a branch with no upstream. Push asks for
confirmation first. Buttons are disabled when the action can't apply
(detached HEAD, merge/rebase in progress, no upstream, upstream deleted on the
remote, nothing to push) and don't appear in exported snapshots.

## Run it

```bash
cd ~/repos/repo-dashboard

./repodash.py serve            # live dashboard on http://127.0.0.1:8787/
./repodash.py serve --open     # ...and open a browser
./repodash.py export           # self-contained repo-status.html snapshot
./repodash.py scan             # terminal report
```

Root defaults to this directory's parent (`~/repos`). Override with `--root` or
`REPODASH_ROOT`. Extra git repos (or folders of repos) come from
`extra-repos.txt`, then `REPODASH_ALSO` (colon- or comma-separated), then
repeatable `--also PATH`. Missing extra paths are skipped.

`gh` enrichment is skipped for `mythos-ai` and `mythosDylan` remotes: the CLI
on this machine is a personal-account login and must not be used against those
orgs.

To keep it always-on, see `repodash.service` (systemd user unit, loopback only).

## What it tracks

**Core git state** — current branch, detached HEAD, staged/unstaged/untracked
counts, merge conflicts, ahead/behind origin, missing upstream, no remote,
stashes, extra worktrees.

**Commit activity** — last commit time, author and subject; when git last
touched the repo at all.

**Agent-development markers** — this is the part that matters for keeping tabs
on agent work:

| Signal | Why it matters |
|---|---|
| **On agent branch** | The repo is *currently checked out* on a `ccode/`, `claude/`, `codex/`, `cursor/`, `aider/`, `devin/` or `copilot/` branch. An agent session was left mid-flight. |
| **Abandoned agent branches** | Agent branches nobody is on that hold unpushed commits, or were never pushed at all. These accumulate silently. |
| **Stale `index.lock`** | A git process died mid-operation. Every later git command in that repo fails until the lock is removed. Locks younger than 2 minutes are reported as live activity instead. |
| **Interrupted operations** | An unfinished rebase, merge, cherry-pick, revert or bisect. |
| **Agent scaffolding** | Untracked `.claude/`, `.cursor/`, `.codex/`, `PR_BODY.md` etc. are recognised as expected clutter and demoted to informational, so they don't drown out real uncommitted work. Tune the list in `scan.py` → `NOISE_UNTRACKED`. |
| **Instrumentation badges** | Which repos carry `CLAUDE.md`, `AGENTS.md`, `.claude/`, MCP config, GitHub Actions. |

**PR / CI** — optional, via the `gh` CLI. If `gh` is missing or not
authenticated the columns switch off and a banner says why; nothing else is
affected. Results are cached in `~/.cache/repodash/gh.json` for 5 minutes
(`--gh-ttl`). CI is labeled as the **latest workflow**, not an aggregate of all
required checks. A successful run is green only when its SHA matches the local
commit and the cache is not stale. PR counts ending in `+` are limited results.
No credentials are read or stored here — auth is entirely `gh`'s.
Disable with `--no-gh`.

## Reading the dashboard

Severity runs **Critical → Serious → Attention → Info → Clean**, and the default
sort is worst-first. Every status is an icon *and* a word, never colour alone.

The stat tiles are clickable filters. `/` focuses search, `r` refreshes, `e`
expands all, and clicking any row opens detail: all flags, branch breakdown,
abandoned agent branches, changed files, open PRs, and a **Suggested next step**
box with the relevant git command. **Those commands are printed, never run.**
Read them before pasting — some are destructive in the wrong repo.

## Files

| File | Role |
|---|---|
| `scan.py` | Git data collection and the classification rules. Start here to add a signal. |
| `extra-repos.txt` | Home-directory clones scanned in addition to `--root`. |
| `ghinfo.py` | Optional `gh` enrichment, cached and fully degradable. |
| `repodash.py` | CLI, HTTP server, static export. |
| `ui.html` / `app.js` | The dashboard. Inlined into one file on export. |
| `test_scan.py` | Extra-root discovery and Mythos `gh` skip. |

`git_actions.py` implements the protected fast-forward. `processes.py` owns
command process groups and timeout cleanup. `test_audit.py` contains the audit
regressions, and `test_app.js` exercises frontend behavior.

## Automation

`scan --exit-code` exits 1 when any repo needs action, with either text or JSON
output. Discovery failures exit 2 and include structured `errors` in JSON.
Failed Git queries mark a repository incomplete and block its write actions.
The command drops into cron or
a scheduled task:

```bash
./repodash.py scan --only-action --exit-code || notify-send "repos need attention"
```

Refresh the shareable snapshot on a schedule:

```bash
*/15 * * * * ~/repos/repo-dashboard/repodash.py export --out ~/repos/repo-dashboard/repo-status.html
```

## Security notes

- The server binds `127.0.0.1` by default. The page exposes local paths, branch
  names and commit subjects, so `--host 0.0.0.0` prints a warning — you almost
  certainly don't want it.
- A `Host` header check rejects non-loopback hostnames, which blocks
  DNS-rebinding against the loopback bind.
- `POST /api/git` (the Pull / Push buttons) also requires an `X-Repodash: 1`
  header and a loopback `Origin`, so another site open in your browser can't
  trigger a push: the custom header forces a CORS preflight the server never
  answers. It acts only on paths from the current scan, one action per repo at
  a time. It rescans the repo right before running git and refuses if the
  branch differs from the one shown on the page. When Origin is present it must
  match the request's scheme, hostname, and port exactly. Git runs without stdin,
  terminal or graphical credential prompts. Its 120s action deadline terminates
  the process group, including ordinary hook/transport descendants.
- Pull locks the checkout's actual index, including in linked worktrees, and
  prepares a Git ref transaction before updating the working tree. Concurrent
  checkout/commit cannot redirect the mutation. It never runs a merge or rebase.
- Remote URL userinfo, query parameters, and fragments are removed from public
  scan data and exported payloads. Do not put secrets in repository names,
  commit messages, filenames, or arbitrary hook output: these are still user data.
- Forced refresh requests require a local client and `X-Repodash: 1` with the
  same Origin checks as actions. Refresh work is serialized/coalesced and requests
  have a read deadline. Ordinary reads still expose metadata when binding to the network.
- HTML and JSON exports each use a unique temporary file and atomic replacement.
  Concurrent writers cannot interleave either file; the HTML/JSON pair is not a
  multi-file transaction.
- All rendered values are HTML-escaped; the export escapes `<` inside the JSON
  payload so repo content can't break out of the `<script>` block.
- The scanner uses `git --no-optional-locks` and was verified across 7 full
  scans of 39 repos to create zero `index.lock` files.

## Verification status

For the October 23, 2026 audit fixes, run:

```bash
python3 -m unittest -v
node test_app.js
```

The Python HTTP integration tests need permission to open loopback sockets.
All mutation fixtures use temporary local bare remotes. See `docs/VALIDATION.md`
for the current result and unverified environments, and `docs/STATUS.md` for
remaining work. The following initial implementation checks are historical:

Verified against the real 39 repos in `~/repos`: scan correctness spot-checked
against raw `git` output, both themes screenshot-reviewed, no console errors, no
horizontal overflow, server endpoints and the DNS-rebinding guard exercised.

`[Unverified]` The `gh` PR/CI path could not be exercised end-to-end — `gh` was
not installed in the environment this was built from. The degradation path (gh
absent) is tested and correct; the success path is written but unproven. If the
columns misbehave once `gh` is authenticated, that is where to look.
