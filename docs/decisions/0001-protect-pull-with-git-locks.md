# ADR-0001: Protect Pull with Git's ref transaction and index lock

- **Date:** 2026-10-23
- **Status:** accepted
- **Deciders:** Implementation decision under the user's request to fix the audit bugs.

## Context

The audited command checked a branch, then launched `git pull`. Another Git
checkout could change the local destination after validation. An application
mutex cannot coordinate other terminals/editors/agents using the same checkout.
The existing Pull feature should remain available with fast-forward-only behavior.

## Decision

Fetch the explicit upstream to a unique temporary ref. Acquire the checkout's
canonical index lock, prepare a native Git `update-ref --stdin` transaction for
the expected old OID, then verify the locked HEAD still names the expected branch.
Use a private index for `read-tree -m -u`, commit the prepared transaction, and
publish the private index while the canonical index remains locked. Refuse tracked
changes or divergent ancestry. Verify a lost acknowledgement against the named
ref before restoring/publishing the index. Do not reset or force the checkout.

The design uses [Git's reference transaction protocol](https://git-scm.com/docs/git-update-ref)
and the [two-tree working-tree update](https://git-scm.com/docs/git-read-tree).
The protocol and linked-worktree behavior are exercised with Git 2.34.1 on this host.

## Alternatives considered

- Repeat the branch check immediately before `git pull`: leaves the same race.
- Use only an application mutex or worktree administrative lock: does not prevent
  a normal concurrent checkout from redirecting a pull.
- Disable Pull: safe containment, but unnecessarily removes the feature once the
  native locking path is established and tested.
- Implement custom ref lock files: duplicates Git's reference backend and reflog
  handling; use the native prepared transaction instead.

## Consequences

Pull is a controlled fetch/fast-forward rather than the porcelain `git pull`
command. It requires a clean tracked working tree. It does not run merge/rebase or
post-merge hooks; Git fetch/reference hooks can still run. Extra command lifecycle,
index publication, and rollback tests are required. The implementation currently
targets POSIX with Git 2.34+; Windows would need different process/pipe handling.

## Revisit if

Git changes locking/backend guarantees, support extends beyond POSIX, or the tool
needs to integrate dirty working trees or full merge/rebase/hook semantics.
