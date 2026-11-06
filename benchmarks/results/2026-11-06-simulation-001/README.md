# Numerical parameter sweep across PC and laptop

Measured November 6, 2026, Toronto time. This evaluates a small generated
scientific-model workflow, not customer data or an industrial simulation.

## Result

Each batch contains eight independent oscillator simulations, with two million
RK4 steps each and a 20-second simulated interval. Position and velocity are
checked against a separate closed-form oracle, using absolute and relative
tolerances of 1e-7. See the [workload contract](../../simulation-workload.md).

| Profile | Median eight-task batch, s | Validated tasks / measured batch second | Task p50 / p95, s |
|---|---:|---:|---:|
| Direct PC sequential | 2.8041 | 2.8485 | 0.3369 / 0.3592 |
| Direct PC parallel, two processes | 1.3767 | 5.7902 | 0.3301 / 0.3404 |
| Forge, PC plus laptop | 2.0458 | 3.8944 | 0.3226 / 1.1017 |
| Forge, one PC worker, separate invocation | 2.6924 | 2.9809 | 0.3177 / 0.3620 |

The cluster took 27.04% less batch time than PC sequential execution but 48.61%
more time than two direct PC processes. The local-parallel baseline won; the
sequential comparison alone must not be presented as a general advantage of
distributed execution. One job per worker uses one payload process on each
machine, not all of their CPU cores.

The separate single-PC-worker reference took 2.6924 seconds; cluster time was
24.02% lower. These fleet modes were not interleaved. Its own sequential baseline
was 2.8537 seconds. Small Forge/direct sequential differences do not establish
negative dispatch overhead: the profiles include different input/result handling,
and host load/cache state was uncontrolled.

The two completed experiments validated all 240 executions (40 warmup and 200
measured): 144 in the physical comparison and 96 in the separate PC reference.
No trials in these completed experiments failed or were discarded. Each profile
has five measured batches and only 40 task-latency observations; tail estimates
are limited. The small model has an analytic answer, so the deliberately fixed
RK4 resolution is an execution example, not the most efficient solution method.

## Unavailable-worker run retained

An earlier invocation is preserved under unavailable-fleet. The laptop had exited
with I/O deadline exceeded before measurement; the user supplied that terminal
output. The cause of the underlying stall was not established. Forge currently
does not automatically reconnect that worker. A still-running SSH session alone
does not establish that its worker remains connected.

The runner was launched despite its initial inspection showing the laptop DEAD.
That orchestration mistake should have stopped the launch. Each Forge trial's
own capability check rejected the unavailable fleet; no Forge task was submitted.
The invocation finished invalid, with six failed Forge trials, 40 unobserved
measured tasks and no cluster performance or speedup values. It executed 96 valid
direct baseline tasks, retained separately and excluded from the table above.
It completed before a stop request could signal it.

The laptop was manually restarted. Both expected workers were then explicitly
checked READY before a fresh invocation with a new directory. That successful
invocation retained every trial. Reconnecting after an unavailable fleet is not
evidence of automatic worker reconnection, and does not erase the initial failure.

## Conditions and provenance

- Control/coordinator/PC worker: native Ubuntu WSL2 on an Intel i7-14700K, Python
  3.12.3. Laptop: native Ubuntu WSL2, Intel Core Ultra 5 226V, Python 3.14.4.
- PC-local communication used loopback mutual TLS; laptop traffic used mutual TLS
  inside its key-authenticated, restricted SSH tunnel over apartment Wi-Fi.
- The unchanged C++ runtime was built from c5ba401 with GCC 13.3 Release and
  OpenSSL 3.0.13. Harness/source checkout was
  1760687280d506a75f2f0c06137de85ae8e6c3ed. Source labels are not binary attestations.
  PC binary SHA-256: 6aa17a83a6b8d741bd7c873bd88259fd5864126f9c7076d61a66aed8bb343eac.
  Laptop binary hash was not collected.
- The new Python workload was staged by Forge; the laptop did not need a checkout
  update or new dependency installation. Python was already installed. Input
  parameter files were transferred without shared workload storage.
- Neither worker enabled cgroup enforcement. Power mode, CPU placement/frequency,
  thermals and background activity were not controlled. Development services were
  not stopped. The user was asked to keep the laptop awake, but power settings
  were not independently inspected. Hardware, Python versions, input placement
  and transport differences limit causal interpretations.
- Across measured cluster tasks, PC compute median was 0.2926 seconds (29 tasks)
  and laptop compute median 0.7208 seconds (11 tasks). This is an observation of
  different assigned cases, not a controlled processor comparison.
- Cluster phase medians: queue 2 microseconds, reservation-to-write wait 44.5
  microseconds, worker execution 0.3197 seconds. These overlapping per-host
  intervals are not pure scheduling overhead or one-way network latency.
- One warmup and five measured rounds per profile, shuffled with seed 20261106.
  Experiment order was unavailable fleet, single-PC reference, valid physical
  comparison. No cross-invocation interleaving was performed.
- Batch time includes map, staging, execution and result-file persistence;
  generation/oracle, cluster startup and post-run phase checks are excluded.
  See [timing definitions](../../README.md). Process CPU intervals are not host
  utilization measurements.

Original metadata reports git_dirty=true. Windows Git and Linux Git with
core.autocrlf=true were clean before measurement and report publication;
diff --ignore-cr-at-eol was empty. Linux default line-ending interpretation
explains the flag, which remains unchanged. Binary and workload hashes were
rechecked. The nine raw metadata/trials/summary files are unchanged exports.
workload-source.py preserves the standalone workload; review.py handles LF/CRLF
checkouts when checking that snapshot's recorded hash.

## Reproduce and review

Use the [simulation guide](../../simulation-workload.md), the recorded configuration
and an idle two-worker physical fleet. Require both desktop-native and
laptop-native READY before launch. Use --workload oscillator --steps 2000000
--tasks 8 --repetitions 5 --warmups 1 --seed 20261106 --collect-phases. Run the
managed single-PC reference separately with --workers 1 and no external host.
Use new output directories and retain failures. Actual machine/transport changes
must be disclosed; this is not a portable performance threshold.

```sh
python3 benchmarks/results/2026-11-06-simulation-001/review.py
```

The independent review reconstructs raw answers with complex exponentials and
recomputes medians, throughput and nearest-rank percentiles, including the invalid
fleet run. There is no new recovery, external-user, adoption, GPU/ARM64 or sandbox
claim from this experiment. Automated reconnect and simpler onboarding remain open.
