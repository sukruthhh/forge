# Job inspection and retained logs

Run these against the same coordinator used to submit work:

    forge ps
    forge logs job-1
    forge timings job-1

Both accept --port PORT. ps shows ID, STATE, WORKER, EXIT, and a bounded command
summary. summaries replace control/non-ASCII bytes with ? for display and are not
a shell-escaped reconstruction of argv. stdout/stderr in logs remain binary-safe.

timings prints JSON durations in seconds for queue, dispatch wait/write,
assignment-to-terminal, and worker prepare/execute/cleanup/result-wait phases.
It accepts --host and --port, requires the same authentication as other inspection,
and uses the same active-job/history retention. Unfinished or unavailable phases
are null, including legacy worker telemetry. These are per-host monotonic durations;
some intervals overlap. See [protocol.md](protocol.md) for precise boundaries.

## States and semantics

- QUEUED: awaiting a compatible idle worker.
- ASSIGNED: reserved on a worker, waiting for its next heartbeat.
- RUNNING: assignment is being delivered; not an exec-start acknowledgment.
- SUCCEEDED: worker reported exit 0.
- FAILED: worker reported nonzero exit (including launch failure).
- TIMED_OUT: worker reported an explicit execution timeout.
- LOST: worker session failed before an accepted result.
- UNSCHEDULABLE: no placement within five seconds.
- CANCELLED: submission interrupted by coordinator shutdown; not a cancellation API.

Exit is shown only for command results. Worker loss is not a synthetic command
exit code, and a normal nonzero command result is distinct from infrastructure loss.

Completed stdout/stderr and timeout/truncation flags are retained for the latest
64 jobs transferred into history, ordered by submission-handler finalization.
Active jobs remain in a separate bounded map. History evicts the oldest finalized
entry, including its arguments and output. It is lost on coordinator restart.
There is no disk persistence, live log streaming, automatic retry, or follow mode.

logs returns 0 when a retained command result is retrieved, even if that command
originally exited nonzero. run still returns the original command's exit code.
Unknown/evicted IDs and unfinished jobs produce explanatory nonzero errors.
LOST/UNSCHEDULABLE jobs retain their infrastructure diagnostic; logs returns it as
an error rather than inventing stdout or an exit status.

## Wire protocol additions

This milestone introduced types 10 through 12; later authentication/staging
extensions are described in [protocol](protocol.md). Framing is unchanged:

| ID | Name | Fields |
|---|---|---|
| 10 | JOBS | None |
| 11 | LOGS | One job ID |
| 12 | JOB_LIST | Repeated groups: ID, state, worker or -, exit or -, summary |

JOBS receives JOB_LIST; LOGS receives the retained RESULT or an ERROR.
Snapshots copy data under the registry lock; socket I/O happens afterward.
128 active submissions plus 64 retained entries produce at most 960 fields,
below the 1024-field limit. Bounded labels/summaries keep the frame below 64 KiB.
Per-stream output limits remain 16 KiB. History adds bounded process memory;
it does not change resource enforcement or authentication.
