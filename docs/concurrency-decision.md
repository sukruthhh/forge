# Decision: use spare worker capacity for independent batch tasks

## Problem and evidence

The two-host prime and oscillator experiments both lost to direct two-process
execution on the PC. They did not establish that every workload benefits from a
cluster. Source review identified a concrete restriction: Forge admitted only one
job per worker, regardless of available CPU/RAM. That left capacity unavailable
to independent tasks and made a useful next hypothesis to test.

This was engineering evidence, not customer research. No user-demand, onboarding
or adoption outcome is claimed.

## Choice and constraints

Add an explicit 1..16 concurrent-job limit under one worker identity. Keep one
as the default. Admit jobs only within aggregate requested CPU/RAM capacity;
preserve individual attempts, staged inputs, cancellation and optional cgroups.
Expose active reservations so users can inspect the configuration.

Reject registering multiple full-capacity identities for one host: that would
multiply advertised resources. Reject fixed resource partitions: large jobs
could be refused despite enough aggregate capacity. Defer broader scheduling
policies until bounded concurrency has evidence.

Acceptance criteria were overlapping independent jobs, no reservation overcommit,
correct per-input results, cancellation without killing a sibling, retries for
all lost attempts, and cleanup before reconnect. Kernel tests additionally require
one cgroup OOM to leave its sibling alive. A performance gate is a measured
comparison, not a hard-coded CI speed threshold.

## Result and interpretation

The first controlled experiment improved median batch time only 16.25%. Timing
records exposed the active-heartbeat delay before spare slots received new work.
Refining bounded pending heartbeats addressed that restriction.

The final same-binary one-/two-slot comparison reduced median eight-task time
**50.12%, from 2.9427 to 1.4679 seconds**. Throughput increased from **2.7343 to
5.4289 tasks/s**. All 192 executions validated, including direct baselines and
warmups. Direct parallel execution still took 1.4093 seconds, and per-task p95
did not improve. Both experiments and the independent reviewer are
[published with limitations](../benchmarks/results/2026-11-06-concurrency-001/README.md).

This supports optional concurrency for the tested one-host numerical workload.
It does not establish physical-cluster scaling, customer value, secure isolation,
or performance on arbitrary commands. The same capacity mechanism is now available
for the next physical-fleet experiment, which must rebuild/update those workers
and record new measurements before any such claim.

The product judgment is to improve useful capacity within existing safety rules,
retain the stronger baseline and unsuccessful first result, and describe the
remaining overhead rather than equating extra workers with guaranteed speedup.
