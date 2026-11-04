# TCP protocol v1: registration and liveness

Implemented on Linux. Plaintext is loopback-only; configurable IPv4 endpoints use
mutual TLS 1.3 as described in [network-setup.md](network-setup.md). Optional shared-token
admission is described in [authentication.md](authentication.md). Job submission
and process execution are described in [execution.md](execution.md), which extends
this registration/liveness protocol with message IDs 7..9.

## Encoding

All integers are unsigned and big endian. Each frame is:

```text
u32 payload length (6..65536 bytes, excludes these four bytes)
u8  version = 1
u8  message type
u32 number of fields (0..1024)
repeated fields:
    u32 byte length
    raw bytes
```

Fields are binary-safe at the framing layer. Schema validation applies separately.
Unknown versions/types (outside 1..30), excess fields, trailing bytes, truncated fields, and
out-of-range lengths are errors. Reads/writes handle fragmentation and coalescing.
Each receive shares one absolute monotonic deadline across header and body;
receiving a few bytes does not reset it. Sending suppresses SIGPIPE and uses a
deadline too. Connection attempts are limited to three seconds.
TLS adds a separate three-second handshake deadline. Framing and application
message IDs are identical inside TLS; no plaintext negotiation or fallback exists.

## Messages

| ID | Name | Fields |
|---|---|---|
| 1 | REGISTER | ID, OS, architecture, CPU decimal, RAM bytes decimal, CUDA `0`/`1`; optional ordered extensions `staging-v1`, `longpoll-v1`, `timing-v1`, `attempt-v1` |
| 2 | HEARTBEAT | None; belongs to the registered connection |
| 3 | CLUSTER | None; first message on a separate client connection |
| 4 | OK | None |
| 5 | SNAPSHOT | Repeated groups of seven fields: the six registration fields plus state |
| 6 | ERROR | One diagnostic string; connection closes afterward |
| 13 | AUTHENTICATE | One shared token; required first when FORGE_TOKEN is configured |

Registration IDs, OS, and architecture labels are 1..64 ASCII letters, digits,
dots, underscores, or hyphens. CPU is 1..1048576; RAM is a positive uint64.
The stock worker detects Linux architecture and system CPU/RAM totals and
conservatively advertises CUDA=0. It does not account for cgroup limits or CPU
affinity yet. Claimed resources are not enforced or authenticated.

## Sessions and bounds

A client must register, submit a job, or request a snapshot within three seconds. Registration
receives OK. The worker sends a heartbeat approximately every 500 ms and waits for
OK (or an assignment) before sending the next. Idle workers advertising longpoll-v1
send the next heartbeat immediately: the coordinator holds its response for up to
500 ms, waking when scheduling assigns work. Running workers keep the 500-ms
heartbeat interval and receive immediate OK responses. Completion wakes the
worker's connection-owning thread rather than waiting for the next heartbeat.
A registered session has a two-second receive deadline.
Any unexpected message is rejected. There is no automatic reconnect loop yet.

Snapshot states use elapsed time since registration or the last valid heartbeat:
READY below one second, SUSPECT from one to two seconds, DEAD at two seconds or
after session disconnect. Status is calculated when inspecting the registry;
heartbeats also drive scheduling. Worker-loss recovery requires explicit retry
eligibility; see [recovery.md](recovery.md). Disconnect cleanup marks the record
disconnected. Duplicate connected IDs are rejected even if their heartbeat has
expired until the old handler closes, preventing stale cleanup from corrupting
the replacement. Disconnected IDs may reconnect.

The coordinator admits at most 96 simultaneous connection handlers. Completed
handler slots are reused and joined; extra connections are closed. The registry
retains at most 64 distinct worker IDs for the coordinator's lifetime, including
dead records. Reuse an old ID or restart to clear that developmental limit.
Snapshots fit within frame/field bounds at this limit. Registry operations use a
mutex; no network I/O occurs while holding it. Job execution state and reservation
ownership are described in the execution document.

SIGINT/SIGTERM set a lock-free stop flag. The accept loop checks it periodically;
handlers finish or time out and are joined before registry destruction. Registry
state is in memory and lost on restart. Thread-per-connection favors simplicity
at this scale; an event loop is a future option if measurements justify it.

Job listing and log retrieval add message IDs 10..12; see [job-inspection.md](job-inspection.md).

## Staging and capability discovery

| ID | Name | Fields |
|---|---|---|
| 14 | STAGED_SUBMIT | CPU, RAM, timeout, OS, architecture, file count, file triples, argv |
| 15 | STAGED_ASSIGNMENT | Job ID, timeout, file count, file triples, argv |
| 16 | WORKER_CAPABILITIES | None; first application request after any authentication |
| 17 | CAPABILITY_SNAPSHOT | Repeated eight-field groups: six worker capability fields, state, staging capability |

Each file triple is basename, executable `0`/`1`, binary file data. Count is 1..16,
total data is at most 32768 bytes, and filenames are flat ASCII labels excluding
`.` and `..`. Duplicate names, path separators, NUL, unknown flags, invalid counts,
and oversized packages are rejected before admission. Complete submission and
assignment frames must fit the existing payload/field bounds; the coordinator
checks assignment framing using the maximum job-ID length before allocating a job.
Job IDs/argv are not filenames and retain their existing independent validation.

The optional registration feature is exact: `staging-v1`; any other seventh value
is rejected. Six-field legacy registrations are accepted with staging disabled.
Snapshot type 5 retains its original schema. Type 17 additionally reports BUSY for
a recently heard-from worker with an assignment, and uses an empty capability
string for legacy workers. Its other liveness states match type 5.

Only staging-capable workers may receive type 15. Updated join advertises the
feature; it requires an updated coordinator. Older coordinators reject these
extensions rather than silently executing without files. Authentication gates
all new application requests. See [batch workloads](batch-workloads.md).

## Immediate dispatch and phase inspection

Registration accepts six through ten fields. Extensions occupy exact positions:
seventh staging-v1, eighth longpoll-v1, ninth timing-v1, tenth attempt-v1. Six/seven-field workers
retain immediate heartbeat replies and ordinary RESULT messages. Eight-field
workers use held idle heartbeats with ordinary results. Nine-field workers must
send PROFILED_RESULT. Ten-field workers send ATTEMPT_RESULT instead. The stock
worker advertises all four extensions and requires
an updated coordinator; there is no fallback to an older coordinator.

| ID | Name | Fields |
|---|---|---|
| 18 | TIMINGS | One job ID; authenticated inspection request |
| 19 | TIMING_SNAPSHOT | Job ID, state, eight duration fields below |
| 20 | PROFILED_RESULT | Original six RESULT fields, then worker prepare, execute, cleanup, and result-wait durations |

Durations on the wire are unsigned decimal microseconds; each worker interval
must be at most 60 seconds. TIMING_SNAPSHOT fields are queue, dispatch wait,
dispatch write, assignment-to-terminal, worker prepare, worker execute, worker
cleanup, worker result wait. Empty fields mean unavailable, including unfinished
intervals and worker telemetry for legacy workers or lost jobs. Ordinary client
results and retained logs still use the original six-field RESULT schema.

Coordinator durations use its monotonic clock: queue admission to reservation;
reservation to assignment write start; assignment write duration; reservation to
terminal transition. Worker durations use its own monotonic clock: assignment
receipt through staging/async startup; the execute call including process/output
handling; workspace destruction; cleanup completion to result construction on the
connection thread. Result wait excludes socket write/ACK time. Worker values are
reported telemetry, not independently authenticated execution evidence. Clocks
are never subtracted across hosts. Intervals overlap and cannot all be summed;
these do not measure one-way network latency or isolate pure scheduling cost.

## Opt-in recovery and attempt identity

| ID | Name | Fields |
|---|---|---|
| 21 | RETRY_SUBMIT | CPU, RAM, timeout, OS, architecture, retries, argv |
| 22 | RETRY_STAGED_SUBMIT | CPU, RAM, timeout, OS, architecture, retries, file count, file triples, argv |
| 23 | ATTEMPT_ASSIGNMENT | Job ID, attempt, timeout, argv |
| 24 | ATTEMPT_STAGED_ASSIGNMENT | Job ID, attempt, timeout, file count, file triples, argv |
| 25 | ATTEMPT_RESULT | Job ID, attempt, exit, stdout, stderr, timeout flag, truncation flag, four worker phase durations |
| 26 | ATTEMPTS | One job ID; authenticated inspection request |
| 27 | ATTEMPT_SNAPSHOT | Job ID, job state, maximum attempts, then repeated six-field rows: attempt, worker, attempt state, exit or '-', elapsed microseconds or empty, diagnostic |
| 28 | RETRY_CAPABILITIES | None; authenticated capability request |
| 29 | RETRY_CAPABILITY_SNAPSHOT | Repeated nine-field groups: six worker capabilities, state, staging feature or empty, attempt feature or empty |
| 30 | JOB_ERROR | Job ID, diagnostic; terminal infrastructure failure for a retry-enabled admitted job |

Retries are 1..3 on types 21/22; zero uses the existing types 7/14. Attempts are
1..4. Other resource, file, argv and frame bounds are unchanged. Admission checks
the largest possible assignment, including its attempt field, even for ordinary
jobs that may be dispatched to an attempt-capable worker. Type 17's eight-field
schema and type 5's seven-field schema remain unchanged. Retry-capable workers
receive types 23/24 for every job, including jobs with retries disabled. Older
workers receive their previously negotiated assignments/results and cannot be
selected for retry-enabled jobs.

Type 25 requires the matching job ID, current attempt and owning RUNNING session.
The coordinator validates phase durations as in type 20, then normalizes accepted
results to type 9 for client delivery and retained logs. Type 30 preserves job
identity on retry exhaustion/queue expiry; rejected submissions still return
ordinary ERROR without inventing an admitted job. Attempts are retained with
active jobs and the latest 64 finalized results, not persisted across restart.
See [recovery.md](recovery.md) for transition rules, time budgets and at-least-once
execution semantics, including possible overlapping processes and side effects.
