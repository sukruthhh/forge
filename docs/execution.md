# First end-to-end execution milestone

## Usage

Start the coordinator and worker in separate terminals in the same Linux environment:

```sh
./build/forge coordinator
./build/forge join --id desktop
./build/forge run --cpu 1 --memory 0 --timeout 5 -- /usr/bin/printf 'hello from Forge\n'
```

`run` waits for completion and returns the command's exit status. Output streams
are separate; CLI diagnostics (job ID, exit, truncation, timeout) go to stderr.
Options precede `--`; subsequent tokens form argv, not an implicitly interpreted
shell command. `--memory` is bytes. A shell can be invoked explicitly if needed.

## Placement and state

SUBMIT creates a process-local `job-N` ID. The CLI requests Linux and defaults to
its own architecture. `--arch native` makes that default explicit; `--arch ARCH`
requests an exact worker-advertised label, such as x86_64 or aarch64. `--arch any`
removes the architecture constraint. GPU requests are not exposed yet. The
existing resource-aware scheduler chooses a connected, recently heard-from, idle
compatible worker. The registry mutex covers selection AND reservation, so
concurrent clients cannot reserve the same slot. Each worker holds one job, even
if CPU remains unused. CPU/RAM reservations are not enforced OS limits.

Jobs transition from QUEUED to ASSIGNED to RUNNING, then to an explicit terminal state. RUNNING means assignment
is being delivered, not that exec succeeded. A terminal state carries a command result or
infrastructure failure. There is no start acknowledgment, durable history, or
attempt/retry state machine. Map iteration follows lexicographic job ID order;
queue fairness/FIFO is not guaranteed.

Worker heartbeats receive assignments. A separate asynchronous execution task
allows heartbeats to continue. Results must match the RUNNING job on that session.
Completion or session loss releases its reservation once. Unexpected/duplicate
results reject the session rather than overwriting accepted output.

At most 128 submissions are retained, also subject to the coordinator's 96 total
connection-handler limit. Unplaced jobs fail after five seconds. Submissions,
heartbeats, results, and disconnects trigger scheduling. The assignment-to-result
deadline is runtime plus five seconds, checked on worker messages; a session
without messages times out independently. Runtime is 1..30 seconds. The CLI
allows 45 seconds for its response. A disconnected client does not automatically
cancel an accepted job. The latest 64 completed results are retained for inspection;
see [job-inspection.md](job-inspection.md). Bounded flat-file staging and client-driven
map are described in [batch workloads](batch-workloads.md); retries remain planned.

## Execution lifecycle

`posix_spawnp` uses explicit argv, the worker's PATH, and inherited environment.
Without --file it uses the worker's working directory. Staged jobs use a private
temporary directory and can bring scripts, inputs, or compatible executables;
system interpreters and dependencies must still be installed on the worker.
See [batch workloads](batch-workloads.md). Stdin is `/dev/null`. Close-on-exec pipes capture
stdout/stderr, drained without blocking. Each stream retains at most 16384 bytes;
excess is discarded and reported, so output fits the 64-KiB frame limit and noisy
children cannot deadlock on full pipes. This is collected output, not streaming.

A new process group contains the child and ordinary descendants. A monotonic
deadline covers both execution and output pipes held open by descendants. Timeout
kills the group and returns 124 plus a timeout flag. Signal termination returns
128+signal; launch errors return 127. Application exit codes can overlap these
conventions. Cleanup kills remaining group descendants before reaping the child.
waitid with WNOWAIT observes exit while retaining the leader PID until group
cleanup, avoiding a PID-reuse race during cleanup.
Graceful worker shutdown or detected coordinator disconnection cancels execution.

SIGKILL or machine failure cannot run cleanup: a worker's child may survive. This
is one reason worker-loss retries are not enabled. Processes deliberately escaping
the group are outside this trusted-workload scope. Cgroups will provide stronger
process-tree lifecycle control later.

Optional shared-token authentication is available through FORGE_TOKEN; see
[authentication.md](authentication.md). Non-loopback IPv4 connections require
[mutual TLS](network-setup.md). Environment filtering, namespaces, and cgroups
do not exist yet.
Loopback is a development restriction, not authentication: local users able to
connect can submit commands as the worker's user when TLS and FORGE_TOKEN are unset.
TLS certificate holders are trusted to submit commands. Capability totals do not
yet account for container resource limits.

## Protocol extensions

Framing in [protocol.md](protocol.md) is unchanged. New v1 message IDs:

| ID | Name | Fields |
|---|---|---|
| 7 | SUBMIT | CPU decimal, RAM bytes decimal, timeout seconds, OS, architecture, 1..128 argv strings |
| 8 | ASSIGNMENT | Job ID, timeout seconds, argv strings |
| 9 | RESULT | Job ID, exit 0..255, stdout bytes, stderr bytes, timeout `0`/`1`, truncation `0`/`1` |

CPU is 1..1048576; RAM is uint64 including zero; timeout is 1..30. OS must be Linux;
architecture is an exact 1..64 character label, or empty for unrestricted architecture.
The CLI resolves native to uname's machine label and encodes any as an empty field;
native and any are CLI keywords, not wire-level wildcard labels. Older coordinators
reject empty architecture fields rather than silently relaxing compatibility.
Executable must be nonempty and argv cannot
contain NUL. Zero memory means no RAM reservation, not enforced unlimited usage.

SUBMIT is the first client message and receives RESULT or ERROR. HEARTBEAT can
receive OK or ASSIGNMENT. Worker RESULT receives OK; the next heartbeat may fetch
another assignment. Session loss yields ERROR to the waiting client, without retry.

## Inspection update
The job-inspection milestone adds bounded in-memory retention and explicit terminal states.
Terminal states distinguish success, failure, timeout, loss, and placement expiry.
See [job-inspection.md](job-inspection.md) for ps/logs and message IDs 10..12.

## Architecture targeting

```sh
forge run --arch native -- /usr/bin/uname -m
forge run --arch aarch64 -- /usr/bin/uname -m
forge run --arch any -- python3 /opt/workloads/analyze.py
```

Exact labels match the worker's uname machine string. No aliases are normalized:
Linux ARM64 commonly advertises aarch64, so arm64 is a different exact request.
An unknown but syntactically valid label waits for compatible capacity and then
expires under the existing five-second queue deadline if no worker matches.
Labels are 1..64 ASCII letters, digits, dots, underscores, or hyphens; empty or
malformed CLI values and repeated --arch options are rejected.

Use any only when the command, files, interpreters, and dependencies already exist
and work on every otherwise eligible Linux worker. Forge does not inspect the
executable, package dependencies, translate machine code, or emulate another
architecture. OS, resource, liveness, and single-job worker constraints still
apply. It remains the caller's responsibility to select a valid workload target.

Simulated worker advertisements test heterogeneous placement. Actual process
execution with native/any is tested on the local Linux architecture; execution on
physical ARM64 hardware remains unverified until the Mac's Linux VM is ready.
