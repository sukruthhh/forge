# Pending-heartbeat dispatch: local before/after observation

Measured November 3, 2026 (Toronto; November 4 UTC). This is a synthetic
short-task runtime comparison on one Docker Desktop host, not physical-cluster
scaling, real-application performance, or a production capacity claim.

## Result

| Profile | Median four-task batch (s) | Validated tasks / measured batch second |
|---|---:|---:|
| Direct sequential | 0.9960 | 4.014 |
| Direct parallel, two processes | 0.5075 | 7.929 |
| Previous Forge, one worker | 3.9948 | 0.996 |
| Updated Forge, one worker | 0.9507 | 4.161 |
| Previous Forge, two workers | 2.0432 | 1.957 |
| Updated Forge, two workers | 0.4943 | 7.954 |

The two-worker median decreased **75.81%**, or **4.13x** old/new median ratio.
The one-worker median decreased **76.20%** (4.20x). These compare the entire
updated runtime with the previous runtime, not the isolated cost of one function.
The update removes timer-driven dispatch/completion waits and adds phase telemetry.
Both versions use the same new harness, identical workload bytes and TLS transport.

Updated two-worker Forge is approximately level with direct local parallel
execution. Its 2.6% lower batch median is not evidence of a general advantage over
direct subprocesses. Throughput differs by only about 0.3%; these statistics use
different definitions (ratio of medians versus total tasks / summed time).
The new two-worker median is 2.02x faster than direct sequential execution here;
this is ordinary parallelism on one host, not a physical distributed speedup.

All 36 planned trials passed: six profiles, one warmup and five measured rounds
per profile. All 144 task executions passed correctness checks: 24 warmup and
120 measured executions, comprising both Forge and direct baseline work. There
were 96 Forge executions and 48 direct executions. No trial was discarded.
Each profile has 20 measured task-latency samples; tail claims remain weak.
Updated two-worker task p50/p95 were 0.2384/0.2490 s versus 1.0019/1.0337 s
for the previous version (see raw summary for full precision).

## Conditions and provenance

- Updated checkout: c0491fba1a6d0a2456f992fffdcd75d169739901.
- Previous runtime: bdde5130859d59b389d363b235be8e187712b244, built from git archive.
- Both Release builds: GCC 13.3, OpenSSL 3.0.13, Ubuntu 24.04 dev image,
  Python 3.12.3. Actual binary digests and CMake/compiler discovery are in metadata.
- Image: sha256:92d7a03b9d411e542655be7206c08c4e9fddbc4da0c5aade226446d799067859.
- One container, two-CPU quota, 2 GiB memory cap; PC CPU is i7-14700K. The visible
  28-CPU affinity mask and system RAM do not override container quota/memory limits.
- Zelta Redis/Postgres development containers remained running on the shared PC.
  Hardware load, CPU frequency and thermal state were not controlled.
- Read-only Windows source bind mount. Inputs/results use native container /tmp.
  Source/input reads and storage boundaries match across profiles. Executable
  locations differ: current binary on the bind mount, previous binary in /tmp.
- Four prime limits: 500000, 510000, 520000, 530000. The synthetic trial-division
  workload is unchanged in logic from the previous commit; the archive uses LF
  and Windows checkout uses CRLF. Every profile actually executes the same staged
  current workload bytes and verifies the same recorded digest.
- Mutual TLS, fresh managed cluster per Forge trial, setup/shutdown outside timing.
  Order shuffled within rounds using seed 20261103. Phase inspection enabled only
  for updated Forge after its timed batches; this adds between-trial activity.

Original metadata reports git_dirty=true. Windows Git was clean before the run;
container Git without the host's global autocrlf setting views CRLF files as
changed. Both diff --ignore-cr-at-eol and status with core.autocrlf=true were clean
before report publication. The original flag has been preserved. Checkout SHAs
and revision labels are not binary attestations; executable/workload hashes were
independently rechecked. Binary builds were verified before committing the source.

## Phase interpretation

For updated two-worker Forge, measured-task median worker execution was 0.2263 s,
prepare 0.000294 s, cleanup 0.000095 s, and completion-to-result-construction wait
0.0000355 s. Coordinator median reservation-to-write wait was 0.01079 s;
reservation-to-terminal was 0.2366 s. These are per-host monotonic intervals with
different boundaries; they overlap and must not be summed. Worker durations are
reported telemetry. Old workers have no phase instrumentation, so this experiment
cannot compare old/new individual phases. No one-way network latency or pure
scheduler overhead is inferred. See [protocol definitions](../../../docs/protocol.md).

## Reproduction and review

Build the current runtime from the updated revision above using the dev image,
Release, and two build jobs. Archive the previous revision on the host:

```powershell
git archive --format=tar -o build/baseline-bdde513.tar bdde5130859d59b389d363b235be8e187712b244
```

Inside a dedicated container with --cpus 2 --memory 2g and the source mounted
read-only at /workspace:

```sh
mkdir -p /tmp/baseline-src /tmp/results
tar -xf /workspace/build/baseline-bdde513.tar -C /tmp/baseline-src
cmake -S /tmp/baseline-src -B /tmp/baseline-build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/baseline-build --parallel 2
python3 /workspace/benchmarks/run.py \
  --binary /workspace/build/release/forge \
  --compare-binary /tmp/baseline-build/forge \
  --comparison-source bdde5130859d59b389d363b235be8e187712b244 \
  --output /tmp/results/paired-001 --environment-kind container \
  --label 'describe actual shared-host background load' \
  --workers 1 2 --tasks 4 --limit 500000 --step 10000 \
  --repetitions 5 --warmups 1 --timeout 30 --seed 20261103 --collect-phases
```

The committed metadata, trials.jsonl (including raw base64 output) and summary
are byte-for-byte copies of the exported observation. Independent arithmetic
checks reproduced trial counts, medians, throughput, p50/p95, phase medians and
old/new ratios. Local regression evidence: all eleven suites in Debug and Release,
plus the four-container TLS test. CI checks functional correctness without a
performance threshold. Full per-trial artifacts are retained locally under
build/benchmarks/2026-11-03-local-002.

A defensible scoped resume statement is: "Reduced median four-task batch latency
76% (2.04 s to 0.49 s) in a two-worker local synthetic TLS benchmark by replacing
periodic worker polling with condition-variable dispatch and completion wakeups."
Physical-machine execution, representative workloads, failure recovery and enforced
resource isolation remain separate work; this result does not establish them.
