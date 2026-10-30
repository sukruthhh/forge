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
