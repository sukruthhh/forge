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
