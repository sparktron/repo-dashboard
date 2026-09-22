# repodash

A live status dashboard for every git repo under `~/repos`, plus extra trees
listed in `extra-repos.txt` (home-directory clones such as `~/mythos` and
`~/mythos-docs`). Built to answer one question quickly: **where did an agent
leave something unfinished?**

Stdlib Python only — no pip install, no dependencies. It reads git state and
never writes to a repo.

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
(`--gh-ttl`). No credentials are read or stored here — auth is entirely `gh`'s.
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

## Automation

`scan --exit-code` exits 1 when any repo needs action, so it drops into cron or
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
- All rendered values are HTML-escaped; the export escapes `<` inside the JSON
  payload so repo content can't break out of the `<script>` block.
- The scanner uses `git --no-optional-locks` and was verified across 7 full
  scans of 39 repos to create zero `index.lock` files.

## Verification status

Verified against the real 39 repos in `~/repos`: scan correctness spot-checked
against raw `git` output, both themes screenshot-reviewed, no console errors, no
horizontal overflow, server endpoints and the DNS-rebinding guard exercised.

`[Unverified]` The `gh` PR/CI path could not be exercised end-to-end — `gh` was
not installed in the environment this was built from. The degradation path (gh
absent) is tested and correct; the success path is written but unproven. If the
columns misbehave once `gh` is authenticated, that is where to look.
