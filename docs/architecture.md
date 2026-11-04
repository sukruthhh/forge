# Architecture decisions

## 001: Linux execution, portable scheduling core

Use C++20 and CMake. Keep worker capability matching and placement separate from
network I/O and OS process APIs. Linux is the first execution target; a macOS
backend is future work. Docker supplies Linux tooling on the current Windows
development machine. It is not the Forge job execution abstraction.

## 002: Deterministic resource-aware placement

Reject unavailable workers, OS/architecture mismatches, missing CUDA capability,
and requests exceeding unreserved CPU or RAM. Of eligible workers, choose the
lowest projected maximum of CPU and RAM utilization; break ties by worker ID.
This is a placement heuristic, not dominant-resource fairness or a prediction of
execution speed. The coordinator must reserve resources atomically with dispatch
before selecting the next job. Reservations are scheduling promises, not OS
enforcement; cgroups v2 will provide enforcement later.

## Next decisions before distributed execution

- Define bounded, versioned TCP message framing and malformed-message handling.
- Define job and execution-attempt state machines before retries. Worker loss can
  leave a process running; automatic retry cannot promise exactly-once effects.
- Define executable/input staging and output ownership. A path on the submitting
  machine is not automatically available on another worker.
- Define authenticated cluster membership before exposing remote execution beyond
  localhost. Initial execution is for trusted workloads.
- Separate heartbeat suspicion from terminal worker loss and reject stale results
  using attempt identity.

These decisions will be explained as their implementations are introduced.

## 003: Bounded Linux TCP sessions before remote execution

Use Linux sockets, poll-based I/O deadlines, RAII descriptor ownership, and a
versioned length-prefixed field protocol. This keeps dependencies small and makes
partial I/O and message boundaries explicit. Binary fields replace the earlier
JSON proposal; [protocol.md](protocol.md) specifies the exact layout.

A bounded set of connection-handler threads owns sockets. A mutex protects the
in-memory registry; network I/O happens outside its critical sections. This
intentionally simple design supports the initial personal-cluster scale without
claiming event-loop performance. Loopback binding limits the milestone until
authentication and execution policy are implemented. Heartbeat state inspection
exists. The execution milestone now integrates scheduler reservations; automatic
job recovery is still future work.

## 004: Single-job workers and synchronous submission first

`forge run` waits for a result. The registry lock covers placement and reservation;
each worker runs one job while a separate execution task allows heartbeats to
continue. Explicit argv, posix_spawnp, dual output pipes, bounded capture, process
groups, and a runtime deadline define the first execution backend. Worker loss
fails a job without retries. There is no durable history or file staging yet.
See [execution.md](execution.md) for wire messages, lifecycle, and limitations.

## 005: Bounded, in-memory completed-job history

Separate active jobs from the latest 64 finalized results. Keep command exit
status distinct from infrastructure loss and inspection-request success.
Snapshots copy metadata under the registry mutex, with all socket I/O outside
the lock. Bounded summaries and row counts fit one protocol frame; retained
output keeps the existing per-stream limits. Restart discards history; durable
storage and streaming remain separate future decisions.

## 006: Authenticate loopback sessions before adding remote endpoints

An optional FORGE_TOKEN gates every connection before registration, submission,
or inspection. Keep one handshake in the shared client connection helper and one
gate in the coordinator handler so commands cannot bypass it. Read coordinator
configuration before listening; reject invalid credentials without mutating state.
Unconfigured loopback development remains compatible with existing clients.

This adds no cryptographic dependency and establishes admission tests, but bearer
authentication alone is insufficient for an exposed network. The token is sent
unencrypted and all holders share permissions. Keep loopback binding until
authenticated encrypted transport and remote endpoint policy are implemented.
See [authentication.md](authentication.md) for operational limits and protocol.

## 007: Mutual TLS before non-loopback endpoints

Use OpenSSL 3 for TLS 1.3 with mandatory certificate verification on both sides.
Clients validate the coordinator's IP SAN; the coordinator requires a trusted
client certificate. Reuse the binary application protocol over a Connection that
owns its socket and SSL object. Handler threads keep exclusive connection
ownership, and the coordinator's immutable TLS context outlives all handlers.

Keep existing frame deadlines across TLS retries and give connect and handshake
separate three-second budgets. Perform handshakes in bounded handler slots, outside
the registry lock. Numeric IPv4 endpoints avoid an unbounded DNS lookup step;
hostnames and IPv6 remain future work. Fail incomplete TLS configuration before
binding or connecting, and reject remote plaintext before opening a session.

This dependency is justified by certificate verification and encrypted transport;
Forge does not implement cryptography. Cluster certificates authorize all
operations, rather than binding worker IDs to principals or adding roles yet.
Optional token admission remains available inside TLS. See
[network setup](network-setup.md) for credential handling, limits, and the pending
physical-machine validation checklist. Earlier loopback decisions above describe
the milestones that preceded this transport extension.

## 008: Explicit architecture policy before portable workload packaging

Keep native architecture matching as the default for submissions. Expose --arch
for an exact advertised label, with native and any as explicit CLI policies.
Resolve native at submission time and represent any with an empty architecture
constraint, which the placement library already supports. Coordinator validation
accepts that empty field while retaining Linux, argv, and resource validation.

Do not infer portability from a command name or script extension. A Python command
can still depend on architecture-specific packages or executables. The caller
selects any deliberately and must provision compatible files/dependencies on each
worker until packaging defines artifact-specific compatibility. Older coordinators
reject the new wildcard submission schema; native/default messages are unchanged.

## 009: Bounded flat staging and client-driven map

Start with 16 flat files and 32 KiB of file data per task, staying within the
existing frame cap instead of adding unbounded upload state. Validate the full
batch before submitting work, then hold immutable captured bytes in client memory.
Preserve executable intent, not arbitrary ownership or source path metadata.
Validate the coordinator's eventual assignment size before admission; its job-ID
prefix can make it larger than the submission even when the incoming frame fits.

Workers advertise staging-v1 in registration. Keep legacy workers usable for
ordinary jobs and exclude them from staged placement. Extended capability queries
report idle/busy state for initial map fan-out without changing cluster's schema.
The snapshot is advisory; individual placement/reservation remains atomic under
the coordinator mutex, and competing clients can still exhaust capacity.

A worker creates a private directory and files inside the asynchronous execution
task. Use posix_spawn file actions for the child's cwd instead of process-wide
chdir, so heartbeats and concurrent runtime code retain a stable environment.
Kill ordinary process-group descendants before best-effort directory cleanup.
This is lifecycle management for trusted commands, not namespace isolation.

Map is a bounded client pipeline over ordinary submissions, with up to 256 inputs
and 16 in-flight tasks, capped by the initial compatible idle staging-worker count.
Preserve input order in indexed binary stream files and JSONL metadata while
allowing completion in any order. Save per-task metadata as work completes. Do not
overwrite output directories, retry unknown outcomes, or stop independent tasks
after a command failure. Stop new submissions if local result storage fails.

This provides an end-to-end batch workflow without claiming durable batch state,
general dependency packaging, output artifact transfer, or at-least-once recovery.
See [batch workloads](batch-workloads.md) for limits, protocol, and remaining work.

## 010: Measure complete validated batches before claiming performance

Add client monotonic submission-to-result timing to map metadata without calling
it dispatch latency. Keep result persistence outside that per-task interval, but
inside the complete batch timing used by the benchmark driver. Compare sequential
and matching local-parallel subprocess execution with Forge profiles on identical
inputs. Compute an independent correctness oracle before timing, then validate
results after the measured batch ends for both baseline and Forge modes.

Retain all planned warmups and repetitions, rotate trial order using a recorded
seed, and invalidate performance summaries when any task/trial or baseline fails.
Preserve negative time reductions instead of selecting favorable trials. Export
raw task streams, provenance, identities, and timing definitions. Start with a
deterministic synthetic CPU workload to expose runtime overhead; real-application
and physical-machine claims require separate experiments. See
[benchmark methodology](../benchmarks/README.md).

## 011: Wake dispatch and completion without sharing a socket

Hold an idle worker's heartbeat response for at most 500 ms using the registry
condition variable, releasing its mutex while waiting. Reservation notifies that
condition variable, so a pending request can receive an assignment immediately.
Running workers keep their heartbeat cadence. A separate condition variable wakes
the worker connection thread on completion. Execution never accesses the socket
or SSL object. This removes timer-driven waits without adding socket writers.

Explicit registration extensions preserve old workers on updated coordinators;
new workers require an updated coordinator. Handler bounds, liveness deadlines,
and single socket ownership remain. Stop checks use bounded waits and deadlines.
Retain monotonic phase durations with job history, leaving unavailable values
null. Normalize extended worker results to the original client/log schema.
Reported worker durations are telemetry, not proof; clocks are not subtracted
across hosts and overlapping intervals cannot be summed. Compare complete old/new
runtimes in shuffled rounds before claiming improvement. See [protocol.md](protocol.md).
