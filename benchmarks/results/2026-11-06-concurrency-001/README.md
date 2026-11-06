# Bounded concurrency: one-host numerical workload

The final experiment reduced median eight-task batch completion time **50.12%,
from 2.9427 to 1.4679 seconds**, by configuring two execution slots instead of one
on the same Forge worker. Both configurations used the same binary. Direct
two-process execution remained faster at 1.4093 seconds: Forge added **4.16%**
relative to that stronger baseline. This is not physical-machine scaling.

## Final results

| Profile | Median batch seconds | Successful tasks/second | Task latency p50 seconds | Task latency p95 seconds |
|---|---:|---:|---:|---:|
| Direct PC sequential | 2.8500 | 2.8275 | 0.3442 | 0.3562 |
| Direct PC parallel, two processes | 1.4093 | 5.6294 | 0.3381 | 0.3558 |
| Forge, one worker / one slot | 2.9427 | 2.7343 | 0.3517 | 0.3867 |
| Forge, one worker / two slots | 1.4679 | 5.4289 | 0.3389 | 0.3904 |

Throughput is measured tasks divided by the sum of measured batch durations,
not the reciprocal of the median. The observed throughput increase was 98.55%.
Per-task p95 did not improve; halving batch completion time does not mean halving
individual task latency. Each profile has only 40 measured latency samples.

## Method and provenance

- Source: `8b978548ef7e43fee6d9f21bc8e0a561b35ca7a6`, clean checkout at launch.
  The native Release binary was compiled from that source during this session.
- Binary SHA-256 for both Forge configurations:
  `6b397e2248c43ad1bb7d16e84baf12f70c57b5309e68b731aa2cd2ce9f3244d8`.
- One physical PC, Ubuntu WSL2, Intel Core i7-14700K, 28 CPUs in process affinity,
  GCC 13.3.0 and Python 3.12.3. All managed workers were local processes, each with
  one identity, on this PC. No laptop, ARM64 VM or GPU participated.
- Mutual TLS, small staged files, eight damped-oscillator parameter sets, each
  taking 2,000,000 RK4 steps over 20 simulated seconds. Generated model inputs
  have a closed-form oracle; they are not a validated customer workload. Direct
  closed-form evaluation would be a more efficient way to solve this example.
- Four profiles, one warmup and five measured batches each, shuffled within each
  round with seed 20261106. All 24 batches were valid: **192 executions**, comprising
  **160 measured and 32 warmup executions** across Forge and direct baselines.
- Batch time includes map startup, admission, staging, command startup, execution
  and result storage. Cluster/certificate startup, oracle preparation and optional
  phase inspection are outside the batch clock. Raw trial commands are retained.
- Independent complex-exponential position/velocity checks agreed with every
  result within the required 1e-7 absolute/relative tolerance. Maximum observed
  absolute state error was 3.8192e-14; this is not a general solver accuracy claim.
- Hosts were not dedicated or thermally controlled. CPU affinity was not pinned
  to a particular core pair. No optional cgroup enforcement was enabled in this
  measurement, and no utilization or pure scheduler-cost measurement is claimed.

The two-slot profile's median reservation-to-dispatch-write start interval was
43 microseconds. This phase includes control scheduling and is not pure scheduler
overhead; it overlaps other reported intervals and should not be summed with them.

## Retained first experiment

`initial/` contains the complete earlier experiment at
`39e390ce6eec4636f458624d3d5e73ad2d60a092`, before active-slot wakeup refinement.
Its same-binary one-/two-slot comparison improved only 16.25%: 2.8666 to 2.4009
seconds, versus a 1.4192-second direct-parallel baseline. All 192 executions
validated. Its binary hash was checked against the saved original executable.

The timing records exposed assignments waiting while a slot was free: workers
still used their 500-ms running heartbeat interval. The refinement uses pending
25-ms heartbeat requests when spare slots coexist with running work, waking on
new assignments and bounding the control thread's wait before a completed result.
It also explicitly marks execution completion before signaling the control loop.

Both metadata files record clean source checkouts. Original provenance fields,
including Git status and local paths, are retained without rewriting.

The experiments were separate invocations. Their initial/final medians are
descriptive comparisons, not an interleaved causal estimate of the dispatch fix.
The final **50.12%** result compares interleaved one-/two-slot configurations
within the final invocation. Both complete experiments are retained: **384 total
correctness-checked executions**, including both sets of baselines and warmups.

One unrelated local Debug benchmark test smoke failed before recording a batch
duration during development. Its temporary fixture lost the diagnostic cause.
Subsequent repeated checks passed, but its cause remains unresolved; see
[the validation caveat](../../../docs/worker-concurrency.md). Neither retained
measurement invocation had failed or discarded trials. Diagnostic smoke runs
are not counted as performance evidence.

## Reproduce and review

Follow the command in [worker concurrency](../../../docs/worker-concurrency.md)
with a fresh output directory. Its default transport is TLS. Rebuild the recorded
source for an implementation comparison; otherwise record the new revision and
binary hash rather than assuming the same result.

```sh
python3 benchmarks/results/2026-11-06-concurrency-001/review.py
```

The standalone reviewer validates raw stdout, independent analytic answers,
workload hash, job counts, slot provenance, binary identity and summary arithmetic
for both experiments. Raw metadata preserves original local paths, not secrets.

Defensible resume wording:

> Prioritized worker concurrency from workload benchmark findings, reducing median
> eight-task batch completion time 50% (2.94s to 1.47s) in a controlled one-host
> TLS numerical benchmark while validating every result against an analytic oracle.
