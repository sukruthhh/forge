# Staged commands and parallel map

Forge can transfer a bounded set of local files with a task, execute the command
in a fresh worker directory, and collect results for a parallel batch. Programs
such as Python and their system dependencies must still be installed on workers.
This is flat-file staging, not a general dependency packager or sandbox.

## Stage a single command

```sh
forge run --file ./analyze.py --file ./input.json --arch any -- \
  python3 analyze.py input.json
```

Repeat --file to send regular files. The worker sees their basenames in its task
directory; it does not receive their original parent paths. Use ./program for a
staged executable, or pass a staged script to an installed interpreter. If any
source execute bit is set, the staged file receives mode 0700; otherwise 0600.
The directory is created with mode 0700. Source symlinks, directories, FIFOs, and
other non-regular files are rejected. No archive extraction is involved.

Limits per task:

- At most 16 files, including shared files and the map input.
- At most 32768 bytes of file data in total, including binary and empty files.
- Flat 1-64 character ASCII basenames using letters, digits, dots, underscores,
  or hyphens; . and .. are invalid. Duplicate basenames are rejected.
- At most 128 command arguments. The complete submission and eventual assignment
  must fit the existing 65536-byte payload limit, including metadata and argv.
- Existing runtime limit 1-30 seconds and retained output limit 16384 bytes per
  stream. Input limits do not impose a disk quota on the running command.

Files are opened without following a final-component symlink and checked with
fstat before a bounded read. Source files may change while read; this is not an
atomic filesystem snapshot. Successfully read bytes are held in the client and
do not depend on later changes to the original files.

Native architecture is still the default. Use --arch any only if the command and
its installed dependencies work on every otherwise eligible worker. Staging an
x86_64 binary does not make it usable on an aarch64 worker.

## Map local inputs

```sh
forge map --host 192.168.1.50 --arch any --file ./analyze.py \
  --jobs 4 --output ./results inputs/*.json -- python3 analyze.py '{}'
```

Run this in Linux with the existing TLS environment configured for remote
endpoints. The shell expands the input glob. Each explicit input file becomes one
job, with all --file files copied into that job's package. A standalone {} argv
token is replaced with ./ plus the staged input basename. Multiple standalone
placeholders are replaced; embedded text such as prefix{} is not a placeholder.
There is no implicit shell interpretation. Options must be before --.

The batch accepts 1-256 inputs. Repeated input paths represent separate tasks;
different tasks may have the same input basename because their directories are
separate. An input basename must not collide with any shared file basename.
The client reads and validates every package and frame before submitting any job.
A preflight failure does not partially submit the batch.

--jobs is a cap from 1 to 16, default 16. The client discovers compatible, idle,
READY workers advertising staging-v1 and caps parallelism by that count and the
input count. No such worker fails preflight. This snapshot does not reserve the
cluster: competing clients or later failures can still cause the existing
five-second placement deadline to expire. Each job reserves capacity atomically
through the existing scheduler. Ordinary task failures do not stop other tasks.

The output directory defaults to forge-map-results. It must be new and its parent
must exist. Existing directories and files are not overwritten. The client writes:

| Artifact | Contents |
|---|---|
| 0001.stdout / 0001.stderr | First input's bounded binary-safe streams |
| 0001.json | First input's metadata, written when its result is saved |
| Other numbered files | Same format, indexed by original input order |
| results.jsonl | All metadata in input order, written after the batch finishes |

Metadata includes index, staged input basename, job ID when known, state, exit
code, timeout/truncation flags, storage completion, elapsed_seconds, stream filenames, and an
infrastructure diagnostic. Command streams are files, not JSON strings. Metadata
diagnostics escape control and non-ASCII bytes; the stderr file preserves bytes.
Inputs with identical basenames remain distinguishable by index and job ID.

elapsed_seconds uses the client's monotonic clock from task submission through
result receipt, including connection/authentication, placement wait, staging,
execution, and result delivery. It excludes result-file persistence and time in
the client's not-yet-submitted backlog. NOT_SUBMITTED has null elapsed time. It
is not a worker execution timer or scheduler dispatch-latency measurement.
The [benchmark harness](../benchmarks/README.md) requires this timing field and
rejects missing/invalid timing data rather than estimating it from output order.

The CLI reports per-task completion on stderr and a final table on stdout. It
returns 0 only if all tasks succeed and results are stored; otherwise 1. A command
failure retains its own exit code in the manifest. Infrastructure errors have no
invented command exit code. Retry-enabled admitted jobs retain a structured job ID
on infrastructure failure; older error responses may identify it only in the
diagnostic. States are SUCCEEDED, FAILED, TIMED_OUT, ERROR, and NOT_SUBMITTED. The last is
used for tasks stopped before submission after a result-storage failure.

On a result-storage failure the client stops starting further submissions, joins
in-flight tasks, and attempts the aggregate manifest. storage_complete=false or
missing/partial artifacts indicate incomplete storage. It does not retry jobs.
These files are not an fsync-backed durable journal. Killing the CLI can leave
only numbered artifacts, with no final manifest; there is no batch-resume API.

## Ownership and failure behavior

Staged jobs use new message types 14/15. Workers advertise staging-v1 in an optional
seventh registration field. Legacy six-field workers can still run ordinary jobs;
the coordinator never sends them a staged assignment. Capability discovery uses
types 16/17 without changing the existing seven-field cluster snapshot.

Each worker stages files inside its asynchronous execution task, so heartbeats
continue. The child changes cwd through posix_spawn_file_actions_addchdir_np;
the worker process never changes its own cwd. The build requires a libc exposing
that function. Ordinary process-group cleanup runs before directory cleanup.
Setup failures are reported as exit 125 with a diagnostic; that numeric value can
also be chosen by an application and is not a separate protocol error category.

Coordinator admission validates names, counts, flags, size, and assignment framing
before scheduling. Files stay in bounded active job state and are cleared before
history retention. Batch workers share no mutable task directory. No network or
filesystem I/O occurs while holding the coordinator registry mutex; bounded frame
encoding checks and in-memory copies do occur there during assignment construction.

Cleanup is best effort. A killed worker can leave its directory or children behind;
cleanup errors are logged on the worker. Trusted commands can access the worker's
other files, network, environment, and credentials and can deliberately escape
their process group. A private working directory does not establish isolation.
Namespaces, cgroups and stronger cleanup remain separate work. Individual jobs
support [explicit cancellation](cancellation.md), including cleanup evidence.

Worker loss or a network error produces a per-input ERROR and the batch continues
where capacity allows. A failed response can leave submission outcome unknown;
The client does not resubmit unknown outcomes. --retries 1..3 explicitly enables
coordinator worker-loss recovery per input with at-least-once semantics; it does
not retry command failures or result-storage errors. See [recovery.md](recovery.md)
for eligibility, attempt inspection and repeated-side-effect risks. Disconnecting the client does
not cancel already accepted jobs. Map is currently client-driven: the coordinator
sees ordinary individual jobs, not a durable batch object or atomic batch admission.

## Evidence and remaining work

Tests exercise binary snapshots, executable modes, permissions, unique directories,
cleanup on success/failure/timeout, limits and path validation, legacy workers,
parallel placement, ordered output association, mixed exits, truncation, worker
loss, single-worker parallelism capping, and injected result-storage failure.
The private-network test supplies source files only to the CLI container and
verifies staged map tasks on both workers over TLS, with no source directory
visible there. These are correctness tests, not performance benchmarks.

Larger streaming uploads, directory/dependency packages, output-file retrieval,
durable batch state, whole-batch cancellation, and physical-machine validation remain
open. Current map collects stdout/stderr, not arbitrary files produced by jobs.

forge cancel JOB cancels one admitted map input, not the batch backlog. A cancelled
input saves state CANCELLED, exit_code 130, cancelled=true and cancellation_cleanup;
other inputs continue. Non-cancelled rows use cancelled=false and null cleanup.
Exit 130 is a Forge cancellation convention, not an invented observed command
exit. Partial streams are retained when available; map returns 1 for any cancelled
input. Unknown cleanup stays explicit, especially after earlier LOST attempts.
