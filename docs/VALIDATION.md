# Validation state

Date: October 23, 2026 (America/New_York)

Base: `d349658`; working-tree fixes on `codex/fix-audit-findings`.

## Commands

- `python3 -m unittest -q`: **69 passed** in the final application tree; scanner,
  action, HTTP, concurrency, and CLI suite, with loopback socket access enabled.
- `node test_app.js`: **15 passed**; frontend VM/DOM-stub suite.
- `git diff --check`: **passed** for the tracked patch. New Python files were
  also checked with `git diff --no-index --check /dev/null <file>` with no
  whitespace diagnostics (exit 1 means the new file differs from `/dev/null`).

Outcomes were recorded after the final code edit. HTTP tests require loopback
socket access; the initial sandbox socket denial was an environmental limitation.

## Demonstrated behavior

- A checkout during fetch refuses the pull without moving either branch.
- A checkout at working-tree update is blocked by Git's locks; only the confirmed
  branch moves, and its index/working tree finish consistent.
- An aborted ref update restores the working tree; a lost commit acknowledgement
  is verified against the named ref before publishing/restoring the index.
- A timeout after the working-tree update restores the original checkout;
  untracked files that would be overwritten are preserved and the pull refuses.
- Linked worktrees use their own HEAD/index and leave the primary branch alone.
- Dirty tracked files and divergent branches are preserved/refused.
- Synthetic remote credentials are absent from scan, API/action, and static export
  output; no real credential is used by those tests.
- Parked ordinary/agent branches with deleted upstreams and unique commits are
  flagged; branches published elsewhere are not falsely called unpublished.
- Failed Git queries/discovery cannot silently report a complete healthy scan.
- JSON and text share exit-status policy; invalid intervals are rejected.
- Timed-out hooks and scanner descendants cannot perform their delayed work.
- Refreshes are bounded/coalesced, clients order server scan generations, and
  successful mutations retain their result if a subsequent scan fails.
- Concurrent export writers publish complete individual files.
- Cross-port/cross-scheme origins and unauthorized forced refreshes are refused.
- Noninteractive Git retains a configured custom SSH command.
- Stale/different-revision CI is not green; limited PR counts and partial GitHub
  data are explicit in the interface.

## Not demonstrated

No live GitHub API smoke test, graphical browser/accessibility run, deployed
service restart, macOS/Windows test, hosted CI run, or history-wide secret scan.
Local test success must not be represented as any of those outcomes.
