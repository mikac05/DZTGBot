# Performance, recovery, and edge-case verification

Date: 2026-08-08

Task: `MASTER_PLAN.md` Phase 9, Task P9-C

Evidence type: offline local verification

## Evidence identity and boundary

- Base HEAD: `03499594a4f8975ae046fa513c9aada7e1c836b6`.
- Remediation state: an uncommitted working tree layered on that base HEAD. The base
  HEAD is not a remediation commit.
- Runtime: CPython 3.12.13 on Windows 11 (`AMD64`).
- Quality tools: Ruff 0.11.0, mypy 1.15.0, and coverage.py 7.6.12 from the local
  project virtual environment.
- Test isolation: all commands in this record used deterministic local fakes,
  temporary files/databases, or mocked transports. No live external mutation was
  requested or performed.
- External Telegram polling, Gemini generation, Jira Server/Data Center calls,
  NetworkManager L2TP/IPsec VPN operation, systemd behavior, and deployment on an
  Ubuntu 24.04 host remain unverified.

The shared identity above agrees across
`docs/reviews/architecture-remediation-verification.md` and
`docs/reviews/security-release-verification.md`: all three reports use the same
base HEAD, uncommitted remediation-tree status, date, CPython 3.12.13 Windows
environment, pending Ubuntu ShellCheck gate, pilot-only posture, and unverified
external-service boundary. The architecture report's accounting of 448 passes plus
one skip is the same unittest outcome as this report's 449 tests run with one skip.

## Result

The complete offline suite, compilation, dependency consistency, scoped lint,
strict typing, focused branch-coverage gates, and repeated recovery/concurrency
matrix passed. This is evidence that the checked working tree satisfies its
offline performance and recovery invariants. It is not evidence of production
capacity, provider correctness against live services, or successful target-host
deployment.

## Complete quality-gate evidence

Commands were executed from the repository root with `PYTHONPATH=src` where shown.
The path to the virtual environment is repository-relative.

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\python.exe -m ruff check src/dztgbot/domain src/dztgbot/services src/dztgbot/infrastructure src/dztgbot/ui src/dztgbot/__main__.py
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Results:

| Gate | Result | Safe evidence |
| --- | --- | --- |
| Dependency consistency | Pass | `pip check` reported no broken requirements. |
| Compilation | Pass | `compileall` completed for `src` and `tests`. |
| Ruff | Pass | Configured correctness rules passed across the new domain, services, infrastructure, UI, and composition-root scope. |
| Strict mypy | Pass | No issues in 29 source files within the scoped new architecture. |
| Complete offline suite | Pass | 449 tests ran in 6.169 seconds; one platform-specific test was skipped. Measured command wall time was 6.718 seconds. |

The single skip is the Windows lack of `O_NOFOLLOW` for the POSIX-specific
credential-store symlink test. The POSIX mode and symlink behavior still require
the configured Ubuntu 24.04 CI/target-host gate.

The strict checker is intentionally incremental. `pyproject.toml` checks the new
domain, service, infrastructure, UI, and composition-root paths with `strict =
true`; it does not blanket-gate legacy facades. Dynamically typed provider/framework
joins and existing integration ignores are narrowly recorded as module-specific
overrides. Ruff likewise records explicit per-file debt rather than silently
including legacy code in a nominally green gate.

## Focused branch coverage

Coverage was measured by running the complete suite with branch tracing enabled:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m coverage erase
.venv\Scripts\python.exe -m coverage run -m unittest discover -s tests -q
.venv\Scripts\python.exe -m coverage report --fail-under=90 --include='src\dztgbot\domain\fsm.py,src\dztgbot\domain\callbacks.py,src\dztgbot\domain\policy.py,src\dztgbot\services\callback_service.py'
.venv\Scripts\python.exe -m coverage report --fail-under=75 --include='src\dztgbot\infrastructure\persistence\workflow_sqlite.py,src\dztgbot\services\submission_service.py'
```

The coverage-backed suite ran 449 tests in 8.291 seconds (9.748 seconds measured
wall time), with the same single platform skip.

| Focused module | Branch-aware coverage |
| --- | ---: |
| `src/dztgbot/domain/fsm.py` | 98% |
| `src/dztgbot/domain/callbacks.py` | 84% |
| `src/dztgbot/domain/policy.py` | 95% |
| `src/dztgbot/services/callback_service.py` | 89% |
| FSM/callback/security aggregate | **91%** (90% required) |
| `src/dztgbot/infrastructure/persistence/workflow_sqlite.py` | 78% |
| `src/dztgbot/services/submission_service.py` | 75% |
| Repository/submission aggregate | **77%** (75% required) |

These are focused incremental gates, not a whole-legacy coverage claim. The lower
persistence/mutation threshold is enforced and visible; it should rise as
additional failure branches receive deterministic tests.

## Repeated recovery and concurrency matrix

The following concentrated matrix was executed three consecutive times in fresh
Python processes:

```powershell
$env:PYTHONPATH='src'
$modules = @(
  'tests.test_integrated_workflow_recovery',
  'tests.test_integrated_concurrency_matrix',
  'tests.test_restart_matrix',
  'tests.test_ambiguous_create',
  'tests.test_submission_recovery',
  'tests.test_attachment_service',
  'tests.test_published_update_conflicts',
  'tests.test_keyed_processor',
  'tests.test_resource_bounds',
  'tests.test_performance_invariants',
  'tests.test_batch_concurrency',
  'tests.test_graceful_shutdown'
)
for ($iteration = 1; $iteration -le 3; $iteration++) {
  .venv\Scripts\python.exe -m unittest @modules -q
}
```

| Iteration | Tests | Test time | Wall time | Result |
| ---: | ---: | ---: | ---: | --- |
| 1 | 44 | 1.606 s | 1.990 s | Pass |
| 2 | 44 | 1.677 s | 2.053 s | Pass |
| 3 | 44 | 1.587 s | 1.974 s | Pass |

This produced 132 passing test executions with no schedule-dependent failure.
The matrix directly exercises the following invariants.

### Mutation and restart recovery

- A timeout after a simulated remote create commit is reconciled without a second
  create dispatch.
- Immediate negative reconciliation is inconclusive; an unknown create outcome
  cannot be blind-retried.
- Submission attempts are durable before provider I/O, and stalled pending
  attempts become unknown without another network dispatch.
- Published updates compute a complete diff, enforce one revision winner, and
  reconcile unknown update outcomes against complete remote fields.
- A partial attachment restart retries only the failed transfer and never
  recreates the issue. Declared oversize attachments are rejected before download.
- Every FSM state round-trips through repository close/reopen, and a consumed
  callback remains consumed after restart.
- Shutdown cancels owned scheduler work, closes the shared Jira client, and cleans
  resources after partial startup failure.

### Identity, ordering, and one-winner behavior

- Cross-chat analyses may complete out of order without cross-workflow mutation.
- Concurrent callback double-clicks and concurrent published-update preparation
  have one winner.
- A revision change makes the exact old callback stale.
- Owner/chat/thread collection scopes remain isolated; concurrent duplicate intake
  and flush operations each have one winner.
- Collection locks are released before analysis and observer I/O.

### Keyed processing and queue bounds

- Same-key work is serial and preserves admission order while unrelated keys make
  progress.
- Synthetic independent work used a processor concurrency limit of four and queue
  bound of sixteen; eight fast workflows completed within the deterministic
  0.5-second test deadline while a slow workflow remained paused.
- A 30-operation synthetic load measured a maximum processor concurrency of three,
  exactly the configured limit, and drained admitted work back to zero.
- Explicit queue-zero/queue-one cases reject overload with fixed feedback.
- A 0.01-second total deadline removes timed-out waiters without breaking the
  remaining same-key chain or leaking key/slot state.
- Close drains already admitted work and rejects new admissions deterministically.

### Provider-resource bounds and recovery

- The focused limiter schedule measured global concurrency no greater than two and
  per-actor concurrency no greater than one for the configured test policy.
- A queue bound of one rejected additional work without exposing queue occupancy.
- A synthetic 36-call Jira/attachment schedule enforced per-resource concurrency
  no greater than four and per-actor/resource concurrency no greater than two.
  Because Jira and attachment pools are independent, their combined measured bound
  is eight by design.
- Total deadlines include queue wait and external work; the deterministic short
  deadline path cleaned admitted, active, and actor-gate counts back to zero.
- A retry budget of two produced exactly three attempts and rejected a caller's
  request to increase that budget.
- A synthetic five-second cooldown recovered automatically and did not become a
  sticky outage.
- Metrics accepted only fixed event/outcome codes and opaque correlation values;
  counters, timers, and recent history remained bounded.

These are deterministic invariant tests, not throughput or latency benchmarks.
Their timings are useful for regression detection on this machine but do not
predict provider latency or target-host capacity.

## CI and ShellCheck status

`.github/workflows/quality.yml` defines the reproducible Ubuntu 24.04/Python 3.12
gate for pinned dependencies, `pip check`, compilation, Ruff, strict mypy, the
complete coverage-backed unit suite, both focused branch thresholds, and
ShellCheck on `scripts/deploy.sh`.

ShellCheck was unavailable in the local Windows environment, so it was not run and
is not claimed as passed here. The checked-in Ubuntu workflow includes both
`shellcheck --version` and `shellcheck scripts/deploy.sh`; that CI job must complete
successfully before release. The Ubuntu workflow itself was not executed as part of
this local record.

## Residual risks and required follow-up

1. Run the checked-in quality workflow in a clean Ubuntu 24.04/Python 3.12
   environment. This is required to cover ShellCheck, POSIX credential permissions,
   symlink handling, deployment-script behavior, and platform-specific SQLite/file
   semantics.
2. Raise branch coverage in `workflow_sqlite.py` and `submission_service.py` above
   the current incremental floor. Their aggregate passes, but the individual
   submission module is exactly at 75%.
3. Treat the synthetic concurrency timings only as invariant evidence. Perform a
   supervised target-host soak/load exercise with approved non-production provider
   fixtures before setting operational capacity expectations.
4. Validate Telegram, Gemini, Jira, VPN, systemd, backup/restore, disk-full recovery,
   and service restart behavior on the target environment. None was exercised here.
5. Preserve the now-confirmed agreement across all three Phase 9 reports when the
   remediation is committed or verification is repeated. A future evidence run
   must update the commit/tree identity, date, environment, counts, and external
   validation boundary consistently rather than carrying these results forward.
6. The architecture remains intentionally single-host. These tests do not establish
   multi-instance SQLite safety or horizontal scaling, which are outside the current
   release scope.

## Verification conclusion

The current uncommitted remediation tree passes the Phase 9 P9-C offline gate for
compilation, dependency consistency, scoped lint and strict typing, full unit
suite execution, mutation recovery, restart durability, concurrency isolation, bounded
resources, shutdown, and synthetic independent-workflow progress. Release remains
conditional on the Ubuntu CI gate and separately authorized target-environment
validation; no live-service readiness claim is made by this report.
