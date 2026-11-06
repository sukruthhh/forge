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

The [reviewed old/new experiment](../benchmarks/results/2026-11-03-local-002/README.md)
now supplies that comparison: two-worker median four-task batch time decreased
75.81%, from 2.0432 s to 0.4943 s, across five measured rounds plus one warmup.
Direct parallel execution took 0.5075 s. All 144 task executions across six profiles
passed validation. This is a scoped local synthetic optimization result; physical
scaling, representative workloads and fault recovery metrics remain unverified.

| Milestone | Completion evidence still required |
|---|---|
| A: actual cross-machine execution | Initial staged execution verified on PC/laptop Ubuntu WSL2 workers with mutual TLS inside SSH; broader failure/connectivity tests, identity binding and ARM64 validation remain |
| B: useful distributed batch workloads | Flat staging and map now work; larger transfers, richer dependency packaging, output-file retrieval, and real-workload physical-machine validation remain |
| C: fault tolerance | Opt-in bounded worker-loss retries, attempt identity/history, stale-result fencing and at-least-once semantics are implemented; persistence, restart recovery, worker reconnect, broader fault injection and recovery-time measurements remain |
| D: Linux isolation | Cancellation, visible affinity/cgroup capacity discovery, delegated cgroup-v2 CPU/RAM caps and cgroup-wide cleanup are implemented; namespaces, credentials/privileges, broader process controls and evaluated security boundaries remain |
| E: product quality | Practical installation/onboarding, reproducible real-workload benchmarks, meaningful CI/integration coverage, clear documentation, independent user feedback |

November 4 recovery validation: twelve Release suites passed (100.65 seconds).
The full Debug suite passed (96.91 seconds), with final execution/recovery checks
after the shutdown fix (28.72 seconds). The final Release four-container TLS test
passed active-job failover and attempt history. These are correctness checks;
no recovery-time metric or physical-machine failure evidence is claimed.

Cancellation now adds queued withdrawal, running CANCELLING/CANCELLED transitions,
retry suppression, attempt-aware stop delivery and explicit cleanup uncertainty.
Fourteen suites passed locally in Release (106.09 seconds) and Debug (106.61
seconds). The TLS bridge fixture checks staged cancellation, workspace cleanup and
attempt history. Earlier lost attempts remain potentially live; no new performance
measurement or secure-isolation claim follows from these checks.

CPU/RAM reservations alone remain accounting. --enforce-limits now requires a
probed cgroup2-v1 worker and positive memory cap. Discovery accounts for affinity,
visible quotas and memory headroom at registration. Workers execute one job at a
time. There is no general dependency packaging, namespace isolation or persistent history.
The [cancellation API](cancellation.md) preserves uncertainty after worker loss.
Worker-loss retries are explicit opt-in; see [recovery.md](recovery.md).
GPU discovery/execution and a native macOS backend remain future capabilities.

Resource validation: sixteen local suites passed in Release (112.15 seconds) and
Debug (112.57 seconds), with additional actual-affinity checks passing afterward.
The private TLS bridge fixture passed. A separate real-kernel fixture verifies
CPU throttling, memory OOM, setsid cleanup, staged map, timeout/cancel, missing and
readonly delegation, migration refusal, a correctly delegated unprivileged user,
setup failure and worker retirement after failed cleanup. Only the disposable
container's cgroup subtree is changed. This is not a performance or sandbox claim.
See [resource-enforcement.md](resource-enforcement.md) for setup and limits.

November 5 resumption: fresh builds passed all sixteen suites in Release
(112.89 seconds) and Debug (111.77 seconds). Real-kernel enforcement checks passed
with both binaries, and the four-container mutual-TLS fixture passed. These
durations describe test runs, not performance results. Remote CI must be checked
for the published commit separately.

## Physical test fleet: initial PC/laptop execution verified

The intended fleet is the Windows PC, Windows laptop, and M3 MacBook Air. On
November 5 both x86_64 Windows machines built Forge in Ubuntu WSL2 and passed
sixteen local suites (the laptop output was supplied by the user). The PC's
fresh native suite took 111.92 seconds and the laptop's 115.54 seconds; these
are correctness-test durations, not benchmark comparisons.

Two concurrent staged jobs succeeded on distinct PC/laptop workers, with input
hashes, unique input values, worker IDs and attempt history checked. The laptop
also submitted a hostname command executed by the PC. Transport was Forge mutual
TLS inside a key-authenticated SSH tunnel, not direct LAN TLS. See the
[raw correctness evidence and limitations](validation/2026-11-05-physical-001/README.md).
The first [physical synthetic experiment](../benchmarks/results/2026-11-05-physical-001/README.md)
now records median eight-task time of 4.5552 seconds across PC/laptop, versus
5.4479 seconds for direct PC sequential and 2.7181 seconds for direct PC parallel
execution. A separate PC-only Forge reference took 5.3034 seconds. All 240
executions across both experiments validated. The cluster was slower than the
stronger local-parallel baseline. Representative workload performance and onboarding
evidence remain pending. Neither physical worker used the optional cgroup backend
in those tests.

Five [controlled PC-worker-process crashes](validation/2026-11-05-recovery-001/README.md)
then recovered on the laptop with LOST/SUCCEEDED attempt history and verified staged
input results. Median signal-to-first-observed-LOST was 7.13 ms (includes inspection
delay); median signal-to-result was 8.1145 seconds, including the probe's deliberate
eight-second replacement workload. These are not heartbeat-silence, physical power-loss,
network-partition or general application recovery measurements. Orphan cleanup was
performed explicitly by the fixture, not guaranteed by Forge. Both fixture preflight
failures before fault injection are retained alongside the five successful crash trials.
The Mac needs an ARM64 Linux VM; on November 3 the user deferred that setup and
asked to continue code development. Bring it up when physical-machine validation
resumes; it is not a completed test or an automatically scheduled reminder.

An x86_64/ARM64 fleet is heterogeneous. Build Forge for each Linux architecture
and use explicit workload compatibility. Follow [network setup](network-setup.md)
when the machines are ready. Do not block independent implementation work while
waiting for that hardware setup, or mark Milestone A fully verified prematurely.
