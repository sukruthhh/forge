# Reproducible benchmark harness

This harness measures correctness-checked synthetic CPU batches. It does not
establish performance on a real application, physical-machine scaling, scheduler
dispatch latency, recovery time, or worker utilization. Negative results and
failures are retained. CI verifies the harness, not a minimum speedup.

## Build and run inside Linux

Requires the Forge build dependencies, Python 3, openssl, and Git for checkout
provenance. The development image supplies these; the runtime needs no new service.

```sh
cmake -S . -B build/release -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/release --parallel 2
mkdir -p build/benchmarks
python3 benchmarks/run.py --binary build/release/forge \
  --output build/benchmarks/run-001 \
  --environment-kind native-linux --label 'describe this machine and background load' \
  --workers 1 2 --tasks 8 --limit 500000 --step 10000 \
  --repetitions 5 --warmups 1
```

Use the actual environment-kind: native-linux, wsl, container, or vm. Run inside
the Linux guest/environment, not Windows PowerShell or native macOS Python. The
output must be a new directory and its parent must exist. No existing results are
overwritten. Release is recommended; the discovered build type is recorded rather
than inferred from the directory name. A Debug or unknown build must be disclosed.

The default starts and cleans up only its own local coordinator and worker
processes. It uses generated seven-day development certificates for mutual TLS,
kept in a temporary private directory outside the results. Global Forge credentials
are not used in managed mode. --transport plaintext gives an explicitly labeled
loopback-only comparison. Neither mode starts Docker or creates physical machines.
Managed cluster startup, readiness, and shutdown are outside batch timings.

Profiles for --workers 1 2 are:

- local-sequential: one direct workload subprocess at a time.
- local-parallel-2: two direct subprocesses, without Forge.
- forge-1: staged map on one managed worker.
- forge-2: staged map on two managed workers.

Additional worker counts add their corresponding local-parallel baseline. The
local baseline uses subprocesses, not CPU-bound Python threads. All profiles use
the same inputs and workload source. Each batch stores binary streams and metadata;
the direct baseline and Forge have different metadata serializers, so this is a
practical end-to-end comparison, not an isolated scheduler microbenchmark.

## Workload and correctness

Each task counts primes from zero through a configured limit using integer trial
division. It is deterministic synthetic CPU work, not a proposed efficient prime
counting service. Before timing, an independent sieve computes the expected answers.
The input grid is limit + task-index * step and is identical across repetitions.
The kernel and oracle also have known-answer checks in CI.

Every successful observation must have the exact task/limit, integer answer,
workload source hash, valid timing fields, host, worker PID, architecture, and
Python version. Forge results must also report success, complete storage, no
truncation, and the expected input/artifact association. All configured Forge
workers must participate; managed mode additionally checks the actual parent PIDs.

Bounds: 1-16 workers per profile, up to 64 inputs with at least as many inputs as
workers, prime limits at most 5 million, 1-30 measured repetitions, 0-5 warmup
rounds, and a 1-30 second task timeout. Tasks that exceed the deadline are failures,
not samples to discard. The whole map CLI also has a bounded deadline; external
jobs already accepted can continue if its client is killed.

## Timing definitions and comparisons

Trial order is shuffled using a recorded seed within each round. Warmup rounds
are recorded and excluded from measured distributions. This reduces fixed order
bias; it does not eliminate caches, thermal drift, host load, or virtualization
effects. A fresh managed cluster is created for each Forge trial. Warmups prime
the overall experiment, not the lifetime of one persistent worker session.

Batch wall time includes task dispatch, process/interpreter startup, source/input
handling, execution, and result-file persistence. It excludes input generation,
the reference oracle, cluster startup/shutdown, and post-run result verification.
The local baseline includes its own thread-pool setup and result writes. Forge
batch time wraps the complete map CLI, including preflight and artifact storage.

Per-task latency measures subprocess invocation through exit locally, and map
submission through result receipt in Forge. Forge's timer includes connection,
authentication, staging, placement wait, execution, and heartbeat/result delivery;
it excludes result persistence. Both exclude time waiting in the client's
not-yet-submitted backlog. They are not pure execution or dispatch latency.
The workload separately reports compute-only wall time and its process CPU time.
The CPU interval brackets the compute kernel, not the full interpreter lifetime;
process_cpu_seconds_total sums those intervals across measured tasks. It is not
cluster CPU utilization.

Summary definitions:

- Batch median: median measured complete-batch duration, with every repetition kept.
- Throughput: total validated tasks / total measured batch seconds for that profile.
- Task p50/p95: nearest rank ceil(p*n) over that profile's measured task latencies.
- Speedup: baseline median / profile median.
- Time reduction: 100 * (1 - profile median / baseline median); negative is slower.

Forge is compared both with sequential execution and with the matching local
parallel baseline. There is no heterogeneous-worker efficiency calculation.
Any invalid task, warmup, repetition, missing trial, incorrect worker participation,
or trial setup/cleanup error invalidates the profile and suppresses its summary
performance and speedup values. An invalid baseline suppresses comparisons against
it. Raw observations, failure counts, and missing-task counts remain available.

Few repetitions produce weak tail/variance evidence. Inspect raw distributions;
do not turn a smoke run or one favorable median into a resume performance claim.

## Reports and provenance

| File | Contents |
|---|---|
| metadata.json | Actual configuration, environment label, Git commit/dirty state, binary/workload hashes, compiler/build type, control-host OS/CPU/affinity/cgroup limits, Python version, measurement boundaries |
| inputs/ | Exact generated input JSON files |
| trials.jsonl | Planned-order trial records, warmup flags, raw durations, expected answers, base64 stdout/stderr, parsed workload observations, worker identities, failures, commands for map, load samples |
| round-* directories | Per-trial streams/metadata; Forge CLI and managed daemon logs |
| summary.json | Profile validity, sample counts, throughput, latency distributions, median comparisons |

Worker observations also record CPU model, Python/architecture, affinity count,
and visible cgroup CPU/memory limits. Hostnames/PIDs identify execution processes;
they do not prove how many physical machines were used. Credential values and
private keys are never exported. No environment-variable dump is taken.

The Git commit identifies the harness checkout at measurement time; it is not
attestation that an arbitrary supplied binary was compiled from that commit.
Rebuild from a clean checkout for a published experiment. The binary digest and
discovered CMake/compiler settings identify the actual executable used.

Keep metadata.json, trials.jsonl, and summary.json together when publishing a
selected experiment; raw task streams are embedded in trials.jsonl. The exact
workload/input generation can be reproduced from the recorded source/configuration.
Keep the full output directory for debugging. This is not an fsync-backed journal
or an automatic resume system. Interrupted/missing trials are invalid, not zero-time
successes. Keep measurement output under ignored build/ until a reviewed experiment
is deliberately published; the watcher excludes benchmarks/results to avoid a
report export triggering compilation during a later measurement.

## Existing external cluster

Configure the ordinary FORGE_TLS_CA, FORGE_TLS_CERT, FORGE_TLS_KEY and optional
FORGE_TOKEN client environment first. Start the intended workers yourself, then:

```sh
python3 benchmarks/run.py --binary build/release/forge \
  --output build/benchmarks/physical-001 \
  --environment-kind wsl --label 'describe control host and the complete worker fleet' \
  --host 192.168.1.50 --workers 2 --cluster-kind physical-machines \
  --worker-python python3 --tasks 8 --limit 1000000 --step 10000 \
  --repetitions 5 --warmups 1
```

External mode accepts one worker count per invocation and requires an explicit
topology declaration. It checks the READY count and refuses a cluster with active
Forge jobs before the trial. Map still handles staging/capability validation.
It does not stop or modify the coordinator/worker processes, enforce idle hardware,
or attest that the topology declaration is true. Record actual machines, OS/VM
allocations, network, background load, power settings, and dependency versions.
Use separate controlled invocations for single-worker and multi-worker fleets.

For physical comparisons, the local baseline runs on the control machine and the
Forge workload runs on the selected workers. Different hardware or Python versions
can explain results; do not attribute all differences to scheduling. A network
partition can leave accepted jobs running; the harness does not retry a failed
trial until it passes or promise exactly-once execution. The bundled workload has
no external side effects, but this is not a general retry policy.

## CI smoke versus measurement evidence

CTest checks the oracle, latency accounting, sample preservation, failure
invalidation, no-overwrite behavior, and a real worker launch failure. The Docker
bridge test exercises external TLS mode across separate containers. Tiny inputs
and --purpose smoke are intentional; CI asserts no speedup threshold. Physical
execution, real-application workloads, recovery metrics, utilization telemetry,
and recruiter-ready performance claims still require separate experiments.

## Published observations

The [initial local overhead observation](results/2026-11-03-local-001/README.md)
preserves every trial of a small Release experiment on one Docker host. Forge was
slower than both local baselines for that short workload. Its original dirty flag
and the cross-platform CRLF/Git explanation remain intact. It is a starting point
for profiling, not a physical-machine or representative-application claim.
