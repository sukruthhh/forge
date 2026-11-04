# Development status

The full Forge product vision remains the goal. Resume-ready results are an
intermediate milestone, not a reduced release scope. Report source review,
simulated placement, container execution, physical-machine execution, performance,
and user evidence separately. Do not invent measurements or treat feature counts
as an objective percentage of completion.

## Current evidence

- C++20/Linux runtime with TCP coordinator, worker registration/heartbeats,
  resource-aware placement, process execution, bounded results, ps/logs, and
  64-result in-memory retention.
- Mutual TLS 1.3 and configurable IPv4 endpoints; optional shared-token admission.
- Commit 09d2666 passed local CTest, the four-container network test, and
  [GitHub Actions](https://github.com/sukruthhh/forge/actions/runs/41711281090).
- Architecture targeting is implemented: native default, exact labels,
  and explicit any for caller-provisioned portable commands. Simulated architecture
  advertisements verify placement; local Linux processes verify execution.
  All eight CTest suites passed locally; physical ARM64 execution remains pending.
- Bounded flat-file staging and client-driven forge map are implemented. Nine
  suites passed locally, including mixed task failures, worker loss, storage-failure
  injection, validation, and legacy-worker gating. The updated Docker bridge test
  passed with inputs available only on the CLI container. See
  [batch workloads](batch-workloads.md) for the precise scope.

## Full product milestones

A benchmark harness now supplies correctness-checked synthetic CPU runs, sequential
and matching local-parallel baselines, managed or existing TLS clusters, raw data,
and explicit failure invalidation. This establishes measurement tooling, not a
physical-cluster speedup or real-application performance result. See
[benchmark methodology](../benchmarks/README.md).
The first [reviewed local observation](../benchmarks/results/2026-11-03-local-001/README.md)
found short synthetic tasks slower through Forge than direct sequential or parallel
execution. It retains all raw trials and provenance caveats; it is not a resume
speedup or physical-cluster result. All ten Debug and Release suites, plus the
Release container network test, passed before that experiment.

Idle workers now use bounded pending heartbeat requests for immediate dispatch,
and execution completion wakes the worker connection thread. Authenticated
forge timings inspection exposes defined per-host phase durations. Legacy worker
results remain supported. All eleven Debug suites (82.81 seconds), eleven Release
suites (82.11 seconds), and the four-container TLS test passed locally. The harness
now supports shuffled complete-runtime old/new comparisons and optional post-batch
phase collection. Performance impact requires an actual comparison experiment.

| Milestone | Completion evidence still required |
|---|---|
| A: actual cross-machine execution | TLS execution on separate physical Linux machines; record command origin, failures, and results |
| B: useful distributed batch workloads | Flat staging and map now work; larger transfers, richer dependency packaging, output-file retrieval, and real-workload physical-machine validation remain |
| C: fault tolerance | Attempt identity, lost-worker requeue policy, bounded retries, stale-result rejection, explicit at-least-once semantics and side-effect risks |
| D: Linux isolation | Namespaces/cgroups v2, enforced CPU/RAM limits, cancellation and descendant cleanup, tested failure behavior and documented security boundaries |
| E: product quality | Practical installation/onboarding, reproducible real-workload benchmarks, meaningful CI/integration coverage, clear documentation, independent user feedback |

CPU/RAM reservations are not enforced limits. Workers currently execute one job
at a time and report system capacity without cgroup/affinity adjustment. There is
no general dependency packaging, retry/cancellation API, isolation, or persistent job history.
GPU discovery/execution and a native macOS backend remain future capabilities.

## Physical test fleet: preparation pending

The intended fleet is the Windows PC, Windows laptop, and M3 MacBook Air. The user
reported installing Ubuntu on the laptop. The PC's reported WSL list contained
docker-desktop only, so its Ubuntu distribution still needs confirmation/setup.
The Mac needs an ARM64 Linux VM; on November 3 the user deferred that setup and
asked to continue code development. Bring it up when physical-machine validation
resumes; it is not a completed test or an automatically scheduled reminder.

An x86_64/ARM64 fleet is heterogeneous. Build Forge for each Linux architecture
and use explicit workload compatibility. Follow [network setup](network-setup.md)
when the machines are ready. Do not block independent implementation work while
waiting for that hardware setup, or mark Milestone A fully verified prematurely.
