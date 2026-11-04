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
