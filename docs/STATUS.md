# Repository status

Updated: October 23, 2026 (America/New_York)

Base revision: `d349658`; fixes are in the working tree on `codex/fix-audit-findings`.

## Current state

All seven confirmed findings in `CODE_REVIEW_REPORT.md` have repairs and regression
coverage. Pull remains available, using a prepared Git ref transaction plus the
checkout's canonical index lock and a private index. Credentials in remote URLs
are redacted. Parked ordinary/agent branches with unpublished work are actionable.
Query/discovery failures are explicit, JSON respects `--exit-code`, and command
timeouts terminate the POSIX process group.

The related risks are addressed with bounded/coalesced refreshes, local forced
refresh admission, request deadlines, exact Origin checks, noninteractive Git,
independent atomic export files, and explicit latest-workflow/revision freshness.
Successful Git results survive a subsequent scan failure.

## Active work

Implementation, final local validation, and diff review are complete: 69 Python
tests and 15 JavaScript tests pass. No commit or push has been made. The original
audit is retained as a dated baseline.

## Known limitations and unverified work

- Live GitHub/credential-provider behavior and graphical browser rendering have
  not been exercised in this remediation session; fixtures cover the logic.
- GitHub CI remains one latest workflow, explicitly labeled; it does not claim
  to aggregate a repository's required-check policy.
- Linux/POSIX behavior is exercised here. macOS and other Git/Python versions
  need a support-matrix check. Windows is not supported by the process-group and
  pipe-locking implementation.
- Non-loopback reads intentionally expose local repository metadata. No external
  authentication or TLS deployment was added.
- Atomic export applies separately to HTML and JSON, not to the pair together.
- The service unit, history-wide secret scan, and dependency CVE audit remain
  outside this change. Broader architectural/feature recommendations are not bugs
  and have not been implemented.

## Next

Review and commit the patch after validation. Before wider deployment, exercise a
real GitHub login, the intended browser, and the service deployment. Keep the
concurrent-checkout, timeout, and failure-injection tests in the validation gate.
