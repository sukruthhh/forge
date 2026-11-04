# Cancelling jobs

Run forge cancel JOB with the usual --host, --port and TLS/token settings.
Exit 0 means accepted, pending or already CANCELLED, not proof every process has
stopped. Unknown/evicted IDs, invalid requests, unsupported running workers and
already-finalized non-cancelled jobs return nonzero. Requests are idempotent while
the job remains retained. Inspect ps, attempts and logs afterward.

## Ordering and state

| State when cancellation acquires the registry mutex | Transition |
|---|---|
| QUEUED | CANCELLED without another dispatch |
| ASSIGNED | Withdraw the undelivered assignment, release reservation, CANCELLED |
| RUNNING | Require cancel-v1, retain reservation, enter CANCELLING |
| CANCELLING | Return pending status without extending the deadline |
| Terminal | Preserve outcome; only already-CANCELLED jobs report success |

Assignment construction and transition to RUNNING share the cancellation mutex.
Assignments already entering delivery use the running path. Result acceptance
uses that mutex too: accepted completion wins over a later cancellation. If
cancellation wins, the logical outcome is CANCELLED even if the worker finished
naturally before its result arrived. No external effect is undone. Worker loss
never requeues an accepted cancellation, regardless of --retries. Earlier LOST
attempts remain LOST; cancelling between attempts does not invent another attempt.

## Cleanup evidence

| Status | Meaning |
|---|---|
| pending | Awaiting current worker result or session retirement |
| not-started | Assignment not delivered and no earlier LOST attempt |
| reported | Validated owning-worker result after execution/workspace lifecycle, with no earlier LOST attempts |
| unconfirmed | Lost contact/acknowledgement, coordinator shutdown, or an earlier LOST attempt that may still run |
| not-requested | Pre-existing terminal outcome outside this API |

reported is a worker's best-effort lifecycle report, not independent proof that
every descendant or file was removed. Attempt history can show current cleanup
reported while overall cleanup remains unconfirmed due to an earlier LOST attempt.
The reservation stays occupied until a result or session retirement. A five-second
acknowledgement budget is checked on messages; the existing two-second receive
deadline handles silence. Expiration retires the session, reports unconfirmed
cleanup and suppresses retries. These are deadlines, not performance measurements.

## Worker behavior and limits

New workers advertise cancel-v1 after staging-v1, longpoll-v1, timing-v1 and
attempt-v1. Older workers can execute ordinary jobs; running cancellation is
rejected before mutation if unsupported. Queued/undelivered cancellation still
works. New workers require an updated coordinator.

The connection thread alone owns its socket/SSL object. Stop replies carry exact
job/attempt identity; validation precedes setting the execution cancellation flag.
Execution kills the ordinary process group, reaps the leader, drains bounded output
and destroys the workspace before reporting its result. An already-set flag is
checked before spawn. A request racing with spawn is not an atomic no-execution
guarantee. The flag resets only for a new validated assignment.

Process-group escapes, SIGKILL, partitions, permission failures and hostile jobs
remain outside guaranteed cleanup. Workspace removal is best effort. Prior lost
attempts may survive. Namespaces/cgroups remain unimplemented; there is no secure
sandbox or complete descendant-control claim. See [recovery.md](recovery.md).

## Results and map

run returns 130 for explicit cancellation and prints state/cleanup. This is a Forge
convention, not proof the command exited 130. Attempt history retains observed
worker exit when known; otherwise '-'. Normal commands choosing exit 130 remain
FAILED. Cancellation and timeout remain separate outcomes.

Validated partial stdout/stderr and truncation flags retain existing 16-KiB bounds.
logs returns 0 for successful retrieval and prints cancellation metadata. Unknown
worker output is not invented. Map saves CANCELLED, exit_code 130, cancelled=true
and cancellation_cleanup for that input; others continue, including the backlog.
Map returns 1 if any input is cancelled. Whole-batch cancellation is not implemented.
Metadata remains in memory with the latest 64 finalized jobs. Old clients cannot
decode cancellation results; upgrade them before using the new API.

## Evidence

Tests cover queued/assigned/running cancellation, idempotency, completed jobs,
legacy workers, reservation retention, retries, old lost attempts, stale results,
acknowledgement expiry, completion races and shutdown. A real staged map task
spawns a descendant; tests verify group termination, directory removal, partial
streams, manifest/logs, and the next input succeeding on the same worker. A direct
executor test checks pre-existing cancellation prevents external marker creation.
The four-container TLS fixture cancels staged work, checks cleanup/history, then
still exercises worker-loss recovery. These are one-host correctness checks, not
physical-machine, security or cancellation-latency evidence.
