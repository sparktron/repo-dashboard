# REPOSITORY AUDIT: `repo-dashboard`

Review date: October 20, 2026 (America/New_York)

Reviewed revision: `d349658` on `master`

Review type: repository-wide application review, existing test execution, and targeted fault/race reproductions.

Result: **7 confirmed findings; 1 high priority and 6 medium priority.** Additional risks are explicitly distinguished from reproduced bugs. Application code was not changed.

**Remediation update — October 23, 2026:** F1–F7 now have working-tree fixes and
regression coverage on `codex/fix-audit-findings`. Related R1–R4 risks are addressed
by refresh bounds/admission, explicit revision-aware latest-workflow presentation,
atomic exports, exact Origin matching, and noninteractive command execution.
See [current status](/home/mythos/repos/repo-dashboard/docs/STATUS.md) and
[validation](/home/mythos/repos/repo-dashboard/docs/VALIDATION.md). The rest of this
report is the historical audit of `d349658`; its source line references and
“not fixed” statements describe that baseline, not the current working tree.

Final remediation validation: **69 Python tests and 15 JavaScript tests passed**;
the tracked diff and new Python files have no whitespace-check diagnostics.
Live GitHub, graphical browser, and deployed service checks remain unverified.

## 1. Executive Summary

- **What it is:** A local dashboard and command-line tool for scanning Git repositories, identifying work that needs attention, optionally displaying GitHub PR/CI information, and performing individual or bulk pulls and pushes. It also exports a self-contained HTML snapshot.
- **Stack:** Python standard library, Git subprocesses, optional GitHub CLI, browser JavaScript, and HTML/CSS. Python `unittest` and Node's built-in test library cover selected behavior. No third-party Python or frontend runtime package is needed by the reviewed application code.
- **Maturity:** Personal-use tool with useful regression coverage. **[Assessment]** It needs additional safeguards before broader or unattended use of its write actions.
- **LOC:** Approximately 2,460 lines across the five application files, including comments and HTML/CSS; roughly another 750 lines of tests. These are physical line estimates, not executable-statement counts.
- **Top 5 findings:**
  1. **F1 / P1:** A concurrent checkout after validation lets Pull update a different local branch from the one the user confirmed.
  2. **F2 / P2:** Credentials embedded in an origin URL are included in API data and exported HTML.
  3. **F3 / P2:** Parked branches with deleted upstreams can hold unique work while the dashboard reports “clean and in sync.”
  4. **F4 / P2:** Failures in secondary Git queries can become zero counts and a healthy status.
  5. **F5 / P2:** An unreadable scan root produces a `TypeError` instead of a useful scan error.

**Priority definitions:** P1 = address before relying on the affected write action; P2 = schedule promptly because a supported path gives incorrect results or lacks a needed safety property; P3 = lower-impact improvement. No P0 issue was established.

**Validation performed:**

- `python3 -m unittest -v`: **41 passed** after allowing local socket access. The initial sandbox run had 10 socket-permission errors during server setup; those were environment restrictions, not test assertion failures.
- `node test_app.js`: **10 passed**. This command demonstrated the individual JavaScript tests. `node --test test_app.js` also exited successfully but reported only one file-level test in this environment, so it is not the basis for the 10-test claim.
- Seven targeted reproductions confirmed F1–F7. They used temporary repositories, local bare remotes, synthetic credentials, controlled subprocess timing, and injected errors. No real remote was pushed to and no real credential was used in these reproductions.
- Host tools: Python 3.10.12, Git 2.34.1, Node v24.16.0. Results establish behavior on this host, not a browser/OS/version matrix.

**Scope and limitations:** The review covered the seven indexed application/test files: `repodash.py`, `scan.py`, `ghinfo.py`, `app.js`, `ui.html`, `test_scan.py`, and `test_app.js`. All major application paths were examined, with representative test implementations and the complete existing test suites executed. Navigation used jCodemunch as requested. The tracked-file inventory also lists `README.md`, `repodash.service`, `extra-repos.txt`, and `.gitignore`, but the index could not supply their contents; indexing Markdown was explicitly rejected as unsupported. Their documentation, deployment, and configuration contents are therefore **not fully reviewed**. No real GitHub API exercise, graphical-browser accessibility check, load test, dependency CVE database audit, or historical secret scan was performed. Claims below do not imply those checks passed.

## 2. Architecture & Codebase Assessment

**Stack**

The application uses Python's HTTP server, threads, JSON, filesystem APIs, and subprocess APIs. Git is the source of repository state. GitHub enrichment delegates authentication and API calls to `gh`. Browser rendering uses plain JavaScript and strings of escaped HTML. These choices keep installation and the runtime dependency surface small.

**Structure**

- [scan.py](/home/mythos/repos/repo-dashboard/scan.py:180) discovers repositories, collects Git state, applies attention rules, and builds summaries.
- [ghinfo.py](/home/mythos/repos/repo-dashboard/ghinfo.py:178) adds branch-keyed PR/CI data with a disk cache and bounded worker pool.
- [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:204) owns snapshots, action coordination, HTTP routes, CLI commands, and export generation.
- [app.js](/home/mythos/repos/repo-dashboard/app.js:55) owns filters, rendering, request ordering, action feedback, and bulk sequencing.
- [ui.html](/home/mythos/repos/repo-dashboard/ui.html:1) contains the page structure, styling, and initial theme/bootstrap setup.

**Strengths**

- The server admits actions only for paths in its discovered snapshot and re-scans before execution; see [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:352).
- Push uses an explicit source/destination refspec and disables automatic tag following; Pull requests fast-forward-only behavior. Existing tests exercise configuration that would otherwise widen a push; see [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:120) and [test_scan.py](/home/mythos/repos/repo-dashboard/test_scan.py:238).
- Host checks, a custom write header, Origin checks, and loopback-client checks provide several deliberate protections for the local-server use case; see [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:296).
- There is no shell interpolation in the reviewed Python Git execution paths. Argument vectors are passed directly to subprocesses; see [scan.py](/home/mythos/repos/repo-dashboard/scan.py:43) and [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:165).
- HTML text/attribute values are normally escaped, and static JSON replaces `<` before embedding; see [app.js](/home/mythos/repos/repo-dashboard/app.js:30) and [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:189).
- GitHub cache writes use unique temporary files plus replacement, and enrichment failure is isolated from the core dashboard; see [ghinfo.py](/home/mythos/repos/repo-dashboard/ghinfo.py:148) and [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:223).
- The suites cover real local-remote pull/push operations, branch/upstream changes before an action, and frontend bulk-action ordering.

**Weaknesses**

The central weakness is that snapshot validation and Git mutation are separate operations. Per-path locks coordinate dashboard requests, but do not coordinate an editor, terminal, another dashboard process, or another actor changing the same checkout. The data model also fails to distinguish several “unknown because a query failed” states from valid zero values. Those two problems explain the most important findings.

**Maintainability**

**[Assessment]** The repository is still small enough to repair without a framework migration. `scan_repo` combines many independent queries, while the HTTP handler and frontend duplicate action eligibility rules. Targeted extraction of scan results and action policy would improve testability. Splitting every renderer or replacing the stack would add work without addressing the confirmed defects.

## 3. Confirmed Bugs

### F1 — [P1] Pull can update a different branch after a concurrent checkout

- **Confidence:** Confirmed by deterministic race reproduction.
- **Location:** [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:365), [git_action_args](/home/mythos/repos/repo-dashboard/repodash.py:135), and [run_git_action](/home/mythos/repos/repo-dashboard/repodash.py:165).
- **Description:** The endpoint verifies the branch in a fresh scan, then constructs `git pull --ff-only --no-rebase <remote> <merge-ref>`. This pins the fetched source, but Git integrates it into the branch checked out when the command executes. A checkout between validation and execution changes that local destination.
- **Reproduction:** Create a behind `main` and an `innocent` branch at its old commit. Scan/confirm `main`. At the subprocess execution boundary, switch to `innocent`, then allow Pull to run against the local remote. The action returns success, `innocent` moves, and `main` does not.
- **Observed:** `confirmed_branch=main`, `actual_branch=innocent`, `ok=true`, `innocent_changed=true`, `confirmed_main_changed=false`.
- **Why it matters:** Concurrent agent/editor/terminal activity can make the button mutate a branch the user did not select. The existing branch-change test covers a switch before the fresh scan, which does not cover this timing window.
- **Fix:** Treat the expected branch and commit as part of an explicit action contract. Redesign Pull around coordinated worktree/ref updates that enforce that expectation through the mutation, rather than relying only on a prior scan. Rechecking immediately before spawning narrows the window but does not close it. If this cannot be guaranteed promptly, disable the automated Pull action and show the command for deliberate execution until a safe implementation is available. Preserve fast-forward-only behavior.
- **Acceptance test:** Coordinate a checkout after the final validation and before mutation; the action must refuse or affect only the confirmed branch. Assert both branch tips and the working tree. Include individual and bulk actions.
- **Estimated effort:** 1–3 days for a tested redesign; less than a day for temporary containment. **[Estimate]**

### F2 — [P2] Remote URL credentials leak into dashboard data and exports

- **Confidence:** Confirmed with a synthetic credential.
- **Location:** [scan.py](/home/mythos/repos/repo-dashboard/scan.py:219), [HTTP JSON response](/home/mythos/repos/repo-dashboard/repodash.py:404), and [render_page](/home/mythos/repos/repo-dashboard/repodash.py:189).
- **Description:** The complete output of `git remote get-url origin` becomes `remote_url`. The API serializes the whole scan, and export embeds it in HTML. Parsing a clean host/slug does not remove the original value.
- **Reproduction:** Set a temporary repository's origin to `https://audit-user:synthetic-review-secret@example.invalid/org/repo.git`, scan it, and render a snapshot. The synthetic secret is present in both the API-shaped repository data and the resulting HTML.
- **Observed:** `api_contains_synthetic_secret=true`; `static_html_contains_synthetic_secret=true`.
- **Why it matters:** A URL credential that a user configured for Git can be disclosed when an exported snapshot is shared or when a non-loopback dashboard is reachable. Actual exposure depends on credentials being present in the configured URL and someone receiving the data; this reproduction does not establish a real credential incident.
- **Fix:** Redact userinfo and sensitive URL parameters at collection/serialization boundaries. Keep a display-safe remote identifier separate from any operational value. Push/pull already use Git's remote configuration, so the dashboard does not need to publish authentication material. Apply the same rule to HTML export, JSON export, and action responses containing scan data.
- **Acceptance test:** Use synthetic credential-bearing HTTPS and SSH URLs; assert the secret is absent from all response/export bytes while safe host/repository identification still works.
- **Estimated effort:** Less than a day. **[Estimate]**

### F3 — [P2] Unique work on an idle branch with a deleted upstream is missed

- **Confidence:** Confirmed with real temporary repositories and a pruned local bare remote.
- **Location:** [scan.py](/home/mythos/repos/repo-dashboard/scan.py:243), especially the unpushed-branch selection around line 263 and abandoned-agent selection around line 291.
- **Description:** When a tracking branch is gone, Git supplies a nonempty upstream name and `gone=true`, but no ahead count. Idle-branch detection admits `ahead > 0` or branches without an upstream; a gone upstream meets neither condition. The explicit local-only calculation is limited to the current branch.
- **Reproduction:** Push `feature/parked` containing one unique commit; switch back to clean `main`; delete the remote feature branch elsewhere and fetch with prune. Scan the checkout.
- **Observed:** `feature/parked` has one commit reachable from no remote ref, but `unpushed_branches=[]`, `needs_action=false`, and `status=good` with no flags.
- **Why it matters:** The dashboard's core purpose is to find work that might otherwise be forgotten. It presents a healthy result while the only remaining copy of a commit sits on a local side branch. The abandoned-agent calculation has the same gone-upstream blind spot.
- **Fix:** For every branch with no live upstream, including gone upstreams, measure unique local commits or mark the result unknown if measurement fails. Use that result consistently for current, ordinary side, and agent branches. Preserve the existing exemption for branches whose commits are already reachable from another remote ref.
- **Acceptance test:** Cover both ordinary and agent branches with deleted upstreams, with and without unique commits, while a different branch is checked out. Verify row flags, summaries, and `--exit-code` behavior.
- **Estimated effort:** Half a day to one day. **[Estimate]**

### F4 — [P2] Secondary Git query failures become healthy zero values

- **Confidence:** Confirmed with controlled Git query failures.
- **Location:** [local_only_count](/home/mythos/repos/repo-dashboard/scan.py:174) and the secondary queries in [scan_repo](/home/mythos/repos/repo-dashboard/scan.py:210).
- **Description:** Initial repository/status failures populate `errors`, but later operations often discard stderr and substitute defaults. For example, a failed `for-each-ref` leaves an empty branch list, and a failed local-only count returns `0`.
- **Reproduction:** Make only the branch-list query return exit 124 with `timed out`, leaving other queries successful. Separately, make the local-only-count query fail.
- **Observed:** The branch-query case reports `status=good`, `errors=[]`, `branch_count=0`; the failed count returns `0`.
- **Why it matters:** Slow, large, damaged, or temporarily inaccessible repositories can appear to have no outstanding work. A successful process exit and green row cannot then be trusted as evidence of a complete scan.
- **Fix:** Return an explicit success/error result from each query. Model unavailable counts as unknown, not zero. Distinguish required safety data from optional metadata; failed branch enumeration or unique-commit calculation should create an actionable incomplete-scan flag. Do not infer “clean and in sync” from missing safety evidence.
- **Acceptance test:** Inject failure independently into branch enumeration, local-only counts, remote lookup, worktree enumeration, and stash lookup. Assert that safety-critical failures cannot yield a clean status; optional metadata failures remain visible without taking down the dashboard.
- **Estimated effort:** 1–2 days to establish consistent result handling and targeted tests. **[Estimate]**

### F5 — [P2] An unreadable root crashes the scan through an inconsistent return type

- **Confidence:** Confirmed with injected `PermissionError` from directory enumeration.
- **Location:** [discover](/home/mythos/repos/repo-dashboard/scan.py:489), [describe_plain](/home/mythos/repos/repo-dashboard/scan.py:532), and [scan_all](/home/mythos/repos/repo-dashboard/scan.py:559).
- **Description:** On enumeration failure, `discover` returns a dictionary inside the list normally containing plain-directory path strings. `scan_all` passes that dictionary to `describe_plain`, which calls path functions on it.
- **Observed:** `TypeError: expected str, bytes or os.PathLike object, not dict`.
- **Why it matters:** An existing directory can pass the CLI's `isdir` check but still be unreadable. Initial scan/export can terminate; background refresh logs a failure instead of returning a useful diagnostic snapshot.
- **Fix:** Keep discovery return types stable. Return discovery errors in a separate field or a typed discovery result. Include the root error in the scan-level status, preserve the last valid live snapshot where appropriate, and ensure monitoring receives a failure result.
- **Acceptance test:** Cover unreadable/missing root, root removal after startup validation, and an unreadable extra scan directory. Assert structured diagnostics and a meaningful nonzero CLI result instead of a traceback.
- **Estimated effort:** Less than a day. **[Estimate]**

### F6 — [P2] JSON scan output ignores `--exit-code`

- **Confidence:** Confirmed by directly exercising `cmd_scan` with an actionable scan result.
- **Location:** [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:483), compared with the later exit-code check around line 510.
- **Description:** The JSON branch prints data and returns `0` before the `args.exit_code` check.
- **Reproduction:** Supply `json=true`, `exit_code=true`, and a scan result whose summary contains `needs_action=1`.
- **Observed:** Exit status `0`.
- **Why it matters:** Machine consumers using `scan --json --exit-code` receive successful status even when the tool detected work requiring attention. This can suppress a cron/automation failure signal.
- **Fix:** Compute the final exit status independently of presentation format. Print either JSON or text, then return the same status policy. Preserve exit `0` when the failure-status option is not requested.
- **Acceptance test:** Test JSON/text × clean/actionable × flag on/off through the CLI, verifying both valid output and process status.
- **Estimated effort:** Under two hours including tests. **[Estimate]**

### F7 — [P2] Git timeouts leave descendants running

- **Confidence:** Confirmed with a delayed local pre-push hook.
- **Location:** [run_git_action](/home/mythos/repos/repo-dashboard/repodash.py:165) and the generic scanner [run helper](/home/mythos/repos/repo-dashboard/scan.py:43).
- **Description:** The action starts a new session, but timeout handling relies on `subprocess.run` and never terminates the new process group. A hook/helper descendant can survive the parent Git timeout. The generic helper has no descendant cleanup either.
- **Reproduction:** In a temporary repository, install a pre-push hook that sleeps and then writes a marker. Reduce the action timeout for the test. The dashboard reports timeout, then the hook writes its marker afterward.
- **Observed:** `action_ok=false`, `reported_timeout=true`, `child_ran_after_timeout=true`.
- **Why it matters:** The UI and per-path coordination can treat an action as finished while a descendant still holds resources or performs work. A retry may overlap that work. The reproduction confirms descendant survival, not that a real remote push completed after timeout.
- **Fix:** Use a subprocess abstraction that owns the whole process tree. On the supported POSIX host, launch a process group, terminate that group on timeout/cancellation, escalate to a kill after a bounded grace period, and reap the parent. Design a separate equivalent if Windows becomes supported. Apply bounded output handling and the same lifecycle to scanner/GitHub commands where descendants are possible.
- **Acceptance test:** Use local hook/helper descendants to assert none remain after timeout and before the action lock is released. Verify timeout/cancellation results and normal successful commands.
- **Estimated effort:** Half a day to two days depending on supported platforms. **[Estimate]**

## 4. Likely Bugs / Risk Areas

### R1 — [P2] Unbounded forced scans can exhaust local resources

- **Confidence:** Possible; the route and concurrency behavior are established, but no load test was performed.
- **Location:** [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:400), [Store.refresh](/home/mythos/repos/repo-dashboard/repodash.py:216), and [scan_all](/home/mythos/repos/repo-dashboard/scan.py:567).
- **Description:** Each `/api/repos?force=1` request can start a complete scan, and `gh=1` can also bypass the GitHub cache. Snapshot ordering prevents an older scan from replacing the store, but does not bound simultaneous scans. Each scan can create its own worker pool.
- **Failure mode:** A reachable client or accidental retry loop causes concurrent Git subprocesses, GitHub queries, CPU pressure, and slow action responses. Non-loopback reads are intentionally allowed when the server is bound for them; force-refresh is not limited to loopback clients. Request-body reads also lack an explicit per-connection deadline.
- **Investigation:** Add a single shared in-flight refresh or bounded queue, limit forced refresh frequency, require an appropriate local/write-style check for expensive forced work, and enforce request deadlines. Run a bounded concurrency test using stubbed scan work and verify worker/process counts remain capped.

### R2 — [P2] The CI indicator lacks commit and workflow completeness

- **Confidence:** Likely misleading in repositories with multiple workflows or new commits; not tested against GitHub in this review.
- **Location:** [fetch_repo](/home/mythos/repos/repo-dashboard/ghinfo.py:99), [cache_key](/home/mythos/repos/repo-dashboard/ghinfo.py:172), and [ciCell](/home/mythos/repos/repo-dashboard/app.js:211).
- **Description:** CI uses one latest run for the branch. The requested fields omit the run's commit hash, and the cache identity is branch-based. A successful run can therefore be for an older commit or just one of several workflows. The column currently communicates a status without exposing that distinction.
- **Failure mode:** A reader interprets one green run as CI coverage for the checked-out revision even while another workflow failed or the local commit has no run.
- **Investigation:** Include the run SHA and workflow identity, compare with `last_commit.hash`, and label “latest run” versus an aggregate status explicitly. Add fixtures for multiple workflows, pending new commits, stale cache, and detached HEAD.

### R3 — [P2] Concurrent exports share one temporary filename

- **Confidence:** Likely; the fixed-path collision is visible in code, but a concurrent-export reproduction was not run.
- **Location:** [cmd_export](/home/mythos/repos/repo-dashboard/repodash.py:460).
- **Description:** HTML export uses `out + '.tmp'`, so two exports targeting the same output share a file. JSON output is written directly to its final destination.
- **Failure mode:** Overlapping cron/manual exports can truncate/interleave writes or make one replacement fail. Readers of the JSON sidecar can observe a partially written document.
- **Investigation:** Use a unique temporary file in the destination directory for each output, close it fully, and replace atomically. Define whether concurrent writers are serialized or last-writer-wins. Coordinate two writers in a test and verify complete HTML/JSON outputs and temporary-file cleanup.

### R4 — [P3] Same-origin and noninteractive-command claims are stronger than their enforcement

- **Confidence:** Possible operational/security hardening gap; no cross-site write exploit or interactive prompt was demonstrated.
- **Location:** [_origin_ok](/home/mythos/repos/repo-dashboard/repodash.py:312) and [run_git_action](/home/mythos/repos/repo-dashboard/repodash.py:154).
- **Description:** Origin validation checks an allowed hostname rather than matching the server's scheme/host/port. The custom-header/CORS behavior remains a separate protection, so hostname-only matching is not evidence of a browser bypass. Similarly, Git execution sets `GIT_TERMINAL_PROMPT=0` but inherits askpass/SSH configuration despite a docstring promising no askpass.
- **Failure mode:** Deployment through a proxy or a machine with graphical credential helpers behaves differently from the documented assumptions.
- **Investigation:** Define the allowed origin tuples and proxy trust model, test exact Origin behavior, and explicitly specify/test the noninteractive authentication environment. Keep local write restrictions and preflight protections intact.

## 5. Test Coverage Gaps

### Critical

- **Target:** [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:365).
  - **What to test:** Checkout/ref/config changes between validation and mutation, plus surviving descendants after timeout. Assert state of every relevant local/remote ref and the working tree.
  - **Why it matters:** Existing tests prove pre-scan validation and normal operations; they do not establish the safety property in F1 or command termination in F7.
  - **Test type:** Deterministic integration tests with barriers/hooks and local bare remotes.
- **Target:** [scan.py](/home/mythos/repos/repo-dashboard/scan.py:219) and [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:182).
  - **What to test:** Secret redaction in API, nested action data, HTML export, and JSON export using synthetic values.
  - **Why it matters:** Verifies F2 at the actual output boundaries, not just the displayed link.
  - **Test type:** Unit plus HTTP/export integration tests.
- **Target:** [scan.py](/home/mythos/repos/repo-dashboard/scan.py:243).
  - **What to test:** Parked ordinary/agent branches with gone upstreams and unique commits, including the “already on another remote ref” case.
  - **Why it matters:** Completes the current-branch regression coverage around lost-upstream work.
  - **Test type:** Integration tests with real refs and prune.

### Medium

- **Target:** [scan.py](/home/mythos/repos/repo-dashboard/scan.py:174) and [scan_repo](/home/mythos/repos/repo-dashboard/scan.py:180).
  - **What to test:** Per-query timeout, permission failure, nonzero exit, and malformed output; require explicit incomplete status for failed safety data.
  - **Why it matters:** F4 currently lets expected failures masquerade as evidence of no work.
  - **Test type:** Fault-injection unit tests, supported by a few real-Git cases.
- **Target:** [scan.py](/home/mythos/repos/repo-dashboard/scan.py:489) and [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:479).
  - **What to test:** Discovery errors and the JSON/text/exit-code matrix.
  - **Why it matters:** F5 and F6 affect unattended consumers even though the main regression suite passes.
  - **Test type:** Unit and CLI subprocess integration tests.
- **Target:** [ghinfo.py](/home/mythos/repos/repo-dashboard/ghinfo.py:77).
  - **What to test:** Multiple workflows, run SHA mismatch, more than 20 PRs, malformed cache shapes, per-repo API errors, and unavailable authentication with stale cache.
  - **Why it matters:** Current tests emphasize account eligibility and branch-key cache separation rather than completeness and failure presentation.
  - **Test type:** Unit tests with recorded/synthetic `gh` responses; separately marked optional live smoke test.
- **Target:** [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:453).
  - **What to test:** Overlapping exports, failure before replacement, valid sidecar JSON, and no partial publication.
  - **Why it matters:** Establishes the snapshot guarantee behind R3.
  - **Test type:** Filesystem integration tests with synchronized writers.

### Long-tail

- **Target:** [parse_status](/home/mythos/repos/repo-dashboard/scan.py:65) and [parse_remote](/home/mythos/repos/repo-dashboard/scan.py:156).
  - **What to test:** Quoted/control-character filenames, non-ASCII names, renames, submodules, explicit-port SSH URLs, IPv6 hosts, and credential-bearing URLs. Consider NUL-delimited Git output for unambiguous filename parsing.
  - **Why it matters:** The current tests cover paths with spaces and capped noise samples, but do not establish general porcelain/URL parsing compatibility.
  - **Test type:** Table-driven units and focused real-Git fixtures.
- **Target:** [app.js](/home/mythos/repos/repo-dashboard/app.js:644) and [ui.html](/home/mythos/repos/repo-dashboard/ui.html:1).
  - **What to test:** Real-browser keyboard/focus behavior during full row replacement, mobile layouts, blocked storage, theme behavior, failed initial fetch, and static export execution.
  - **Why it matters:** The VM-based frontend tests use a lightweight DOM stub, so browser rendering, focus, and accessibility behavior remain unproven.
  - **Test type:** A small browser smoke/accessibility suite rather than duplicating every unit assertion.

## 6. Improvement Opportunities

### Quick wins (< 1 day, high leverage)

- **Fix output-independent exit status (F6).** Impact: **High**. Effort: **Low**. Move status selection outside the presentation branch; validate through the actual CLI. Source: [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:483).
- **Redact origin URLs (F2).** Impact: **High**. Effort: **Low**. Publish only display-safe values and test every serialization boundary. Source: [scan.py](/home/mythos/repos/repo-dashboard/scan.py:219).
- **Validate numeric options.** Impact: **Medium**. Effort: **Low**. Require a positive background interval, define valid TTL semantics, and validate ports with actionable CLI errors. A zero/negative `stop.wait(interval)` permits immediate refresh repetition. Source: [run_background](/home/mythos/repos/repo-dashboard/repodash.py:262) and [argument definitions](/home/mythos/repos/repo-dashboard/repodash.py:515).
- **Expose partial GitHub results.** Impact: **Medium**. Effort: **Low**. Display per-repo enrichment errors and freshness, and label the PR count as capped/partial when only 20 entries are requested. Current `pr_count` is just the length of that limited list. Sources: [ghinfo.py](/home/mythos/repos/repo-dashboard/ghinfo.py:81) and [ciCell](/home/mythos/repos/repo-dashboard/app.js:211).
- **Make export replacement consistent (R3).** Impact: **Medium**. Effort: **Low**. Reuse the unique-temp/replace pattern already present in [save_cache](/home/mythos/repos/repo-dashboard/ghinfo.py:148) for [cmd_export](/home/mythos/repos/repo-dashboard/repodash.py:453).

### Medium refactors (1–3 days, meaningful cleanup)

- **Separate query results from repository classification.** Impact: **High**. Effort: **Medium**. Extract small, typed query results from [scan_repo](/home/mythos/repos/repo-dashboard/scan.py:180). Preserve unknown/error states through rules and summaries; repair F3–F5 through that shared model.
- **Centralize command lifecycle management.** Impact: **High**. Effort: **Medium**. Give [scanner commands](/home/mythos/repos/repo-dashboard/scan.py:43) and [write actions](/home/mythos/repos/repo-dashboard/repodash.py:154) one tested process-tree timeout/output policy with action-specific limits.
- **Return authoritative action capabilities.** Impact: **Medium**. Effort: **Medium**. Send pull/push availability and a reason with each scan. Let the UI render those, while the server revalidates before mutation. This reduces drift between [git_action_args](/home/mythos/repos/repo-dashboard/repodash.py:120) and [gitBlock](/home/mythos/repos/repo-dashboard/app.js:153), including configurations without an `origin` but with another valid upstream remote.
- **Bound and share refresh work.** Impact: **High**. Effort: **Medium**. Implement one shared in-flight scan and freshness metadata in [Store](/home/mythos/repos/repo-dashboard/repodash.py:204). Return the authoritative latest stored snapshot after overlapping work and include a server scan generation so clients can compare actual snapshot freshness, not just request-start tickets.

### Strategic upgrades (week+, architectural)

- **Make actions explicit, auditable operations.** Impact: **High**. Effort: **High**. Build on [runGitAction](/home/mythos/repos/repo-dashboard/app.js:562) and [the action endpoint](/home/mythos/repos/repo-dashboard/repodash.py:327): expected branch/ref/OID, target identity, operation ID, lifecycle, and a persisted minimal result history. Keep the implementation proportional to a local tool. This should follow immediate F1 containment.
- **Add a small continuous validation matrix.** Impact: **Medium**. Effort: **Medium**. The tracked-file inventory contains no CI workflow or shared validation entrypoint. Run the existing Python and JavaScript suites, the new defect regressions, and an export/browser smoke test on the supported minimum runtime and a current runtime. Do not introduce a large frontend build chain solely for this.
- **Define a support and deployment contract.** Impact: **Medium**. Effort: **Medium**. State supported Python/Git/browser versions, trusted local use, what non-loopback exposure reveals, proxy restrictions, and what “synced” means. Validate the service unit and README separately, since their contents could not be reviewed through the supplied navigation tools.

All effort ranges are planning estimates, not measured implementation durations. No broad framework rewrite is recommended.

## 7. Feature Recommendations

- **Scan completeness and remote-freshness indicators**
  - **Why it fits:** The scanner already aggregates status, errors, and timestamps, but “synced” is based on locally available tracking refs; the scan path does not fetch the remote.
  - **Who benefits:** Users deciding whether a repository is safe to leave unattended or whether a pull is needed.
  - **Implementation outline:** Publish completeness per safety-critical query, the last successful scan, and remote-ref freshness when known. Label ahead/behind as relative to local tracking refs. Offer a separately controlled fetch/prune action with the same safety and concurrency scrutiny as other mutations.
  - **Priority:** **High**, after F1–F7. Sources: [scan_repo](/home/mythos/repos/repo-dashboard/scan.py:180), [pushCell](/home/mythos/repos/repo-dashboard/app.js:127).
- **Bulk-action preview with explicit targets**
  - **Why it fits:** Bulk push already confirms branch/upstream names, but the execution contract and target/freshness information can be made clearer.
  - **Who benefits:** Users running agents across many repositories, particularly when a batch takes long enough for checkouts to change.
  - **Implementation outline:** Preview eligible/skipped repositories, the exact branch and expected commit, sanitized destination, and refusal reasons. Carry that immutable plan into the server action. Add progress/cancellation only after process-tree cleanup is reliable.
  - **Priority:** **Medium**. Sources: [gitActionAll](/home/mythos/repos/repo-dashboard/app.js:599), [do_POST](/home/mythos/repos/repo-dashboard/repodash.py:327).
- **Revision-aware CI detail**
  - **Why it fits:** Optional PR/CI enrichment already exists, and its current one-run model can be extended without changing the core dashboard.
  - **Who benefits:** Users deciding whether a particular checkout or PR is ready for review.
  - **Implementation outline:** Show the run SHA, workflow, age, and link; distinguish a match to local HEAD from older branch activity. Aggregate only workflows explicitly included in a documented policy.
  - **Priority:** **Medium**. Sources: [fetch_repo](/home/mythos/repos/repo-dashboard/ghinfo.py:77), [ciCell](/home/mythos/repos/repo-dashboard/app.js:211).

## 8. Security / Reliability / DX Findings

**Security**

- **Confirmed disclosure condition:** F2 publishes credential-bearing origin URLs. Repair collection/output redaction before sharing exports or exposing the dashboard to additional readers.
- **Confirmed action-integrity problem:** F1 permits a different local branch to move in a concurrent-checkout window. This is a concurrency/safety defect; no remote attacker exploitation was established.
- **Existing protections:** Host validation, custom write header, Origin checks, loopback-client checks, discovered-path admission, explicit push refspecs, and HTML escaping are present in the reviewed code. They are useful defenses, not a proof of complete security.
- **Deployment caveat:** Non-loopback reads intentionally expose repository metadata, and the server prints a warning. See [cmd_serve](/home/mythos/repos/repo-dashboard/repodash.py:431). External authentication/TLS should be designed if that use becomes a supported product direction; do not assume Host validation authenticates readers.
- **Unverified:** Historical secrets, the unindexed configuration/docs, external dependency vulnerabilities, reverse-proxy deployments, and graphical-browser security behavior were not audited.

**Reliability**

- Fix the reproduced false-healthy states, discovery crash, ignored exit status, and surviving process descendants (F3–F7).
- Bound forced scans, make refresh completeness visible, and separate action outcome from any later refresh failure. The current action handler performs `store.refresh()` after the Git action before sending a response; an exception there can obscure a mutation that already happened. Source: [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:379). **[Possible]** No successful-action/failed-refresh reproduction was run.
- Add structured scan/action IDs, duration, and failure categories. Preserve useful diagnostics while redacting credentials. Current HTTP logging is optional and background failures are printed to stderr; see [repodash.py](/home/mythos/repos/repo-dashboard/repodash.py:262).
- Keep exports atomic and make GitHub partial/stale results explicit (R2–R3).

**Developer Experience**

- Both existing suites pass on the reviewed revision, and local bare-remotes keep Git mutation tests deterministic and offline.
- Record `python3 -m unittest -v` and `node test_app.js` as reproducible validation commands, with the local-socket requirement for HTTP integration tests. Establish one CI/validation entrypoint so an apparent file-level Node pass is not mistaken for evidence about all inner tests.
- Add focused regressions for each confirmed finding before implementing its repair. Favor output/state assertions over implementation-mirroring tests.
- Clarify CLI input ranges and return-code semantics. Review the README and service deployment instructions in a follow-up with a navigation method that can access their content.

## 9. Prioritized Action Plan

**Phase 1 — Fix now** (blockers, confirmed bugs, security issues)

1. **Contain F1:** Disable automated Pull if necessary, then close the branch/worktree mutation race with a deterministic regression. Owner role: Git/action maintainer. Exit criterion: the post-validation checkout test never changes an unconfirmed branch. Estimate: containment <1 day; redesign 1–3 days.
2. **Remove F2 disclosure:** Redact remote credentials before API/export data exists. Owner role: scanner/API maintainer. Exit criterion: synthetic secrets absent from every output surface. Estimate: <1 day.
3. **Repair scan truthfulness (F3–F5):** Include gone-upstream side-branch work, propagate critical query errors, and give discovery errors a stable structure. Owner role: scanner maintainer. Exit criterion: unique parked work is flagged and incomplete scans cannot report healthy. Estimate: 1–3 days combined, depending on refactor scope.
4. **Repair machine signals (F6):** Unify exit status across output formats. Owner role: CLI maintainer. Exit criterion: the complete CLI matrix passes. Estimate: <2 hours.
5. **Own subprocess lifecycles (F7):** Terminate descendants and reap before declaring timeout/allowing conflicting work. Owner role: action/runtime maintainer. Exit criterion: hook/helper descendants are gone after timeout. Estimate: 0.5–2 days.

**Phase 2 — Next improvements** (reliability, test coverage, quick wins)

1. Add one bounded, shared refresh operation and request deadlines; verify R1 with controlled concurrency.
2. Make HTML and JSON export writes unique-temp and atomic; test R3 with overlapping writers.
3. Validate numeric CLI options and normalize cache/query error handling.
4. Expose GitHub partial errors, capped PR counts, and revision-aware CI freshness.
5. Add CI and a shared validation command running the existing suites plus all Phase 1 regressions. Test the final repaired tree, not just this audited baseline.

**Phase 3 — Strategic upgrades** (refactors, features, DX)

1. Move toward explicit action plans and minimal operation history if frequent bulk writes remain a core workflow.
2. Add a small real-browser smoke/accessibility suite and a documented runtime support matrix.
3. Review the currently inaccessible README/service/config contents; define supported local/network deployment expectations.
4. Add completeness/remote-freshness and CI-detail features only after the underlying safety results are dependable.

The phases are ordered by risk and dependencies. Estimates assume a maintainer familiar with Git and this application; they are not a delivery commitment. Read-only review completion does not mean these remediations have been implemented.

## 10. Top Patch Targets

1. [repodash.py:120](/home/mythos/repos/repo-dashboard/repodash.py:120) — action contract, branch safety, and command lifecycle; highest risk reduction for F1/F7.
2. [scan.py:180](/home/mythos/repos/repo-dashboard/scan.py:180) — credential redaction, branch completeness, and explicit query errors; addresses F2–F4 together.
3. [scan.py:489](/home/mythos/repos/repo-dashboard/scan.py:489) — stable discovery error model for F5.
4. [repodash.py:479](/home/mythos/repos/repo-dashboard/repodash.py:479) — small, isolated exit-status fix for F6.
5. [test_scan.py:174](/home/mythos/repos/repo-dashboard/test_scan.py:174) — reuse local-remote fixtures for race, redaction, timeout, and parked-work regressions; add a CLI-focused test group.
6. [repodash.py:204](/home/mythos/repos/repo-dashboard/repodash.py:204) and [repodash.py:453](/home/mythos/repos/repo-dashboard/repodash.py:453) — bound refresh concurrency and harden export publication.
7. [ghinfo.py:77](/home/mythos/repos/repo-dashboard/ghinfo.py:77) and [app.js:211](/home/mythos/repos/repo-dashboard/app.js:211) — revision-aware, explicitly partial PR/CI presentation.
8. [test_app.js:19](/home/mythos/repos/repo-dashboard/test_app.js:19) — extend the current VM harness for capability/failure presentation, complemented by actual browser smoke tests.

## 11. Overall Verdict

- **What's solid:** A small dependency-light design, useful status rules, explicit non-forcing push targets, practical local-server checks, and 51 passing existing tests on this host.
- **What's fragile:** The validation-to-mutation boundary, scan completeness semantics, deleted-upstream side branches, credential publication, and timeout/error reporting.
- **Production-ready today?** **With caveats for trusted, supervised personal use; no for broader or unattended reliance on write actions in its current form.** This is an engineering assessment of the reviewed behavior, not a certification. The most important action-integrity guarantee is currently false under a reproduced concurrent checkout.
- **Must change before broader use:** Close or contain F1, redact F2, make F3–F6 truthful for both people and automation, and establish F7's complete termination guarantee. Then run the new regressions and existing suites on the repaired tree and validate the supported deployment/browser environment.

Continuation context: this session produced a review artifact only. No application fixes, commits, pushes, dependency changes, or deployment changes were made. Start remediation with F1 containment and F2 redaction, followed by the scanner correctness group. Preserve the distinction between the seven reproduced findings and the unverified risks when turning this report into issues or patches.
