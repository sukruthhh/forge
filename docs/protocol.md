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
Unknown versions/types (outside 1..17), excess fields, trailing bytes, truncated fields, and
out-of-range lengths are errors. Reads/writes handle fragmentation and coalescing.
Each receive shares one absolute monotonic deadline across header and body;
receiving a few bytes does not reset it. Sending suppresses SIGPIPE and uses a
deadline too. Connection attempts are limited to three seconds.
TLS adds a separate three-second handshake deadline. Framing and application
message IDs are identical inside TLS; no plaintext negotiation or fallback exists.

## Messages

| ID | Name | Fields |
|---|---|---|
| 1 | REGISTER | ID, OS, architecture, CPU decimal, RAM bytes decimal, CUDA `0`/`1`; optional seventh field `staging-v1` |
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
OK (or an assignment) before sending the next. A registered session has a two-second receive deadline.
Any unexpected message is rejected. There is no automatic reconnect loop yet.

Snapshot states use elapsed time since registration or the last valid heartbeat:
READY below one second, SUSPECT from one to two seconds, DEAD at two seconds or
after session disconnect. Status is calculated when inspecting the registry;
heartbeats also drive scheduling, but no automatic job recovery exists. Disconnect cleanup marks the record
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
