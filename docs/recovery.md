# Worker-loss recovery and execution attempts

Forge can retry a job after its worker session is lost. Retries are disabled by
default and require an explicit caller decision:

```sh
forge run --retries 1 --timeout 10 -- /usr/bin/sha256sum /opt/input
forge map --retries 2 --arch any --file summarize.py --output results \
  inputs/a.json inputs/b.json -- python3 summarize.py '{}'
forge attempts job-1
```

Only enable retries for commands whose repeated execution is acceptable. Prefer
pure computations or operations with application-level idempotency keys. Forge
does not inspect commands, roll back effects, deduplicate external writes, or
coordinate transactions with another service.

## Policy and bounds

--retries accepts 0..3 additional attempts; zero is the default. A reservation
creates an attempt numbered 1..4 under a stable coordinator-local job ID. An
attempt records reservation/delivery, not proof that a process started. Each
worker still holds only one job and reservations are atomic under the registry
mutex. Retried jobs require workers advertising attempt-v1, plus staging-v1 for
staged inputs. Legacy workers remain usable for jobs with retries disabled.

The following policies are deliberate:

| Event | Behavior |
|---|---|
| Worker disconnect, receive expiry, assignment/result delivery failure, or rejected worker protocol message | Retire the session, mark its current attempt LOST, and requeue if the job has budget and the coordinator is not stopping |
| Valid success, nonzero command exit, launch/setup failure, or explicit execution timeout | Accept the result and finish; do not retry |
| No compatible capacity | Wait at most five seconds for each queue admission; finish UNSCHEDULABLE if it expires |
| Retry budget exhausted | Finish LOST and retain every attempt |
| Explicit cancellation or coordinator stopping | Prevent further attempts; explicit running cancellation reports cleanup evidence |
| Client disconnect or unknown submission response | No automatic client resubmission; an accepted job can continue |

On loss, the coordinator retires the worker and releases its reservation before
requeueing. Staged bytes remain immutable in active job memory; every dispatched
attempt creates a new private working directory. Requeue resets that queue's
five-second deadline. The execution timeout remains 1..30 seconds per attempt;
the assignment-to-result grace remains timeout plus five seconds. The submission
client allows 45 seconds per allowed attempt, up to 180 seconds, for queueing,
execution and protocol overhead. This is a bounded response wait, not durable job
ownership or a cancellation deadline.

Map preflight counts compatible idle workers supporting the requested policy.
The coordinator still validates actual placement after admission. Competing
clients can consume snapshot capacity. The map pipeline never retries a failed
connection or result write itself; --retries applies to each admitted job inside
the coordinator. Command failures remain independent per input.

## Attempt identity and session ownership

New workers advertise the ordered registration extensions staging-v1,
longpoll-v1, timing-v1, attempt-v1. Assignment and result messages carry both job
ID and attempt number. The coordinator accepts a result only from the session
currently owning that job in RUNNING state with the current attempt number.
Wrong-job, stale-attempt, duplicate and unnegotiated results reject the session.
A rejected session counts as worker loss; its active job may be retried under the
same explicit policy. Invalid results never overwrite accepted output.

Worker IDs can be reused after the previous session retires; attempt numbers
still increase. A new session cannot submit an older attempt's result as the
replacement result. This is result fencing, not a mechanism that stops an old
process from contacting external services. TLS client certificates grant cluster
access; they are not yet bound to worker IDs or distinct operation roles.

## At-least-once semantics and surviving processes

Opt-in retries use at-least-once execution semantics: a lost attempt may already
have executed its command, produced external side effects, or even completed
before its result was lost. It may overlap the replacement attempt. Bounded
retries do not guarantee that any attempt executes or succeeds.

Graceful worker shutdown and detected coordinator disconnection cancel the local
process group. SIGKILL, machine failure or a partition cannot guarantee cleanup.
An old child and its working directory can survive. Attempt fencing only protects
coordinator results, not databases, files or other external effects. Namespace
isolation, durable state, automatic worker reconnect and coordinator restart
recovery remain future work. Configured workers now use cgroups v2 for limits
and descendant cleanup, but worker SIGKILL can still leave orphaned work;
see [resource enforcement](resource-enforcement.md).
There is no exactly-once execution claim.

The [cancellation API](cancellation.md) stops current ordinary process groups when
the worker responds. Prior lost attempts remain uncertain and are not claimed stopped.

## Inspection and output ownership

forge attempts JOB accepts the usual --host, --port and authentication settings.
It prints job state and used/allowed attempts, then one row per reservation:
attempt number, worker ID, state, command exit if known, reservation-to-terminal
duration, and a loss/cancellation diagnostic. Active durations are unavailable,
not fabricated zeroes. A job with no reservation has zero rows.

The active-job registry and latest 64 finalized jobs retain this metadata. Restart
or history eviction discards it. ps retains its existing schema and displays the
current/final worker; it uses '-' while the job is requeued. logs contains only
the accepted final result. Lost-attempt output is not collected or merged with
the replacement's output. Retrying infrastructure errors return a structured job
ID so map error records can refer to attempt inspection; command exit remains null.

forge timings JOB describes the current or last assigned attempt, not whole-job
time across retries. Requeue clears its unfinished intervals and worker telemetry;
previous lost-attempt durations remain visible through attempts. Client run/map
elapsed time includes all attempts and queue waits. Per-host intervals must not
be added together as if they were non-overlapping end-to-end latency.

## Validation scope

The recovery suite exercises explicit opt-in/default loss, all four allowed
attempts, exhaustion, empty replacement capacity, non-attempt-worker exclusion,
malformed retry counts, running/terminal inspection, reused IDs, stale result
rejection, heartbeat receive expiry, and final log retention. A real staged map
test kills its first worker, modifies the original input after admission, and
verifies a fresh worker receives the original bytes and completes. It observes
the surviving old child and repeated side effects, then explicitly cleans up
only that test-owned process group and workspace. Exit 7, launch exit 127 and
timeout 124 stay terminal even with retries enabled.

The private Docker bridge fixture also kills the container owning a running job
and verifies mutual-TLS execution on the surviving worker with LOST/SUCCEEDED
attempt history. These are correctness checks on one host, not physical-machine
failure evidence or recovery-time performance measurements.
