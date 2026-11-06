# Initial physical-cluster synthetic benchmark

Measured November 5, 2026, Toronto time (November 6 UTC). Two physical Windows
machines ran native Ubuntu WSL2 workers. The result demonstrates measured execution
across those machines, not a general distributed-compute speedup.

## Results

Each batch contains eight identical-across-profile prime-counting inputs. The
workload is deterministic synthetic trial division, checked against an independent
sieve. One warmup and five measured rounds were retained per profile.

| Profile | Median batch, seconds | Validated tasks / measured batch second | Task p50 / p95, seconds |
|---|---:|---:|---:|
| Direct PC sequential | 5.4479 | 1.4722 | 0.6608 / 0.7309 |
| Direct PC parallel, two processes | 2.7181 | 2.9351 | 0.6643 / 0.7243 |
| Forge, PC plus laptop | 4.5552 | 1.7702 | 0.6901 / 1.7412 |
| Forge, one PC worker, separate invocation | 5.3034 | 1.5033 | 0.6506 / 0.7219 |

Two-machine Forge took 16.39% less time than PC sequential execution, but
67.59% more time than two direct PC processes. The stronger local-parallel
baseline must accompany any interpretation of the sequential improvement.
This does not establish that adding a laptop is better than using the PC's cores.

The separate single-PC-worker reference took 5.3034 seconds; two-machine time
was 14.11% lower. These fleet modes were not interleaved, so that comparison is
descriptive and cannot isolate the causal benefit of the extra machine. Its own
sequential baseline was 5.4358 seconds. The small Forge/direct sequential difference
is not evidence that dispatching through Forge generally beats direct execution.

Both experiments validated all 240 task executions: 40 warmup and 200 measured.
There were no discarded trials or failures. Each profile has only 40 measured
task-latency samples; p95 estimates are limited. The paired physical experiment
has 18 batches and 144 executions; the single-worker reference has 12 batches
and 96 executions. Neither measures recovery time or sustained capacity.

## Conditions and interpretation

- PC: Intel i7-14700K, Python 3.12.3, GCC 13.3 Release, OpenSSL 3.0.13.
- Laptop: Intel Core Ultra 5 226V, Python 3.14.4. Its earlier user-supplied build
  output reported GCC 15.2 and OpenSSL 3.5.5; no laptop binary digest was collected.
- Each worker runs one job at a time. CPU/RAM requests are placement accounting;
  neither physical worker enabled cgroup enforcement. This does not pool all
  available CPU cores. PC local-parallel uses two processes, not maximum PC capacity.
- The PC hosted the submission CLI, coordinator and one worker. The laptop worker
  connected through its key-authenticated SSH tunnel. Forge used mutual TLS inside
  that tunnel; PC-local traffic used direct loopback TLS. This was apartment Wi-Fi,
  with router isolation unknown. No public router port was opened.
- No shared workload storage was used. Forge staged the same script and eight
  small JSON inputs. Executables/Python were preinstalled on both workers.
- No CPU affinity, quota, power mode, frequency, thermals or unrelated host load
  was controlled. Existing development services were not stopped. User activity
  was not monitored. SSH and WSL are part of the measured environment.
- Inputs were limits 1,000,000 through 1,140,000 in increments of 20,000. The
  seed was 20261105; profile order was shuffled within each round. Cluster setup,
  oracle and post-run inspection were excluded; map and result persistence were
  included. Single-worker setup occurred outside its timed batches.

Across measured physical tasks, PC compute median was 0.6177 seconds and laptop
compute median 1.3356 seconds. Different input assignments, hardware, Python
versions and background activity prevent attributing this difference to one factor.
The physical job phase medians included queue 2 microseconds and
reservation-to-write wait 46 microseconds. These reported, overlapping phases
are not pure scheduler overhead or one-way network latency.

The evidence suggests investigating worker concurrency, heterogeneous completion
times and workload choice before promising a performance advantage. That is an
inference, not a measured diagnosis of a single bottleneck. No representative
application result, adoption metric, GPU/ARM64 result or isolation claim follows.

## Provenance and reproduction

Runtime binary: c5ba401 build, SHA-256
6aa17a83a6b8d741bd7c873bd88259fd5864126f9c7076d61a66aed8bb343eac.
Harness checkout: e900f6520f9313d05fe99ab9f9281a4cfc458099, a documentation-only
successor with unchanged runtime and benchmark source. Source labels are not
cryptographic binary attestations. Full recorded settings and compiler discovery
are in metadata.json; observations and raw base64 task streams are in trials.jsonl.
The pc-one-worker directory preserves the separate invocation's three raw files.
The original files were copied without modifying recorded observations.

Both metadata files report git_dirty=true. Windows Git was clean before the runs;
Linux Git interpreted Windows CRLF files as changed. Before writing this report,
Windows status and Linux status with core.autocrlf=true were clean, and
diff --ignore-cr-at-eol was empty. The recorded dirty flags were preserved.
Binary and workload byte hashes were rechecked. review.py accepts LF or CRLF
checkout copies when checking the published workload's recorded source hash.

To reproduce the physical run, configure a coordinator and idle PC/laptop workers
as described in [network setup](../../../docs/network-setup.md), retaining the SSH
transport and reporting actual environment differences. On the PC, with client
TLS credentials configured and an existing ignored output parent:

```sh
python3 benchmarks/run.py --binary build/wsl-release/forge \
  --output build/physical-validation/new-physical-run \
  --environment-kind wsl --label 'describe actual topology and background load' \
  --host 127.0.0.1 --workers 2 --cluster-kind physical-machines \
  --worker-python python3 --tasks 8 --limit 1000000 --step 20000 \
  --repetitions 5 --warmups 1 --timeout 30 --seed 20261105 --collect-phases
```

For the separate PC-only managed reference, omit --host, --cluster-kind and
--worker-python; use --workers 1 and a new output directory. Do not run the two
experiments concurrently. Independent raw answer and arithmetic review:

```sh
python3 benchmarks/results/2026-11-05-physical-001/review.py
```
