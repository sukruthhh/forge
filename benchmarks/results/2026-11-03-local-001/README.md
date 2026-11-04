# Initial local overhead observation: November 3, 2026

This is a small synthetic observation on one shared Windows/Docker host. It is
not physical-cluster scaling, real-application performance, isolated hardware,
or a favorable resume speedup claim. All planned trials and negative results are
preserved. Read [the methodology](../../README.md) before comparing these numbers.

## Configuration and provenance

- Source checkout: f6fd4d2a50a750c93576b1104262ca2011e7d119.
- GCC 13.3.0 Release, Python 3.12.3, Intel Core i7-14700K.
- One Docker Desktop Linux container, two-CPU bandwidth quota, 2 GiB memory cap.
  CPU affinity still exposed 28 logical CPUs; the quota is not CPU pinning.
- Both Forge worker processes, coordinator, benchmark control, and direct baselines
  shared that container and its resource budget. Docker supplied those constraints;
  this does not implement or verify Forge per-job resource enforcement.
- Other development services were running on the PC. No host isolation, power,
  thermal, or background-load controls were claimed.
- Repository/source was a read-only Windows bind mount. Generated inputs and result
  storage were on the container's native /tmp filesystem. Baseline script reads
  and Forge source capture can still have different filesystem/cache behavior.
- Mutual TLS, four inputs with prime limits 500000, 510000, 520000, 530000.
- One warmup per profile, three measured repetitions per profile, seeded order.
  Sixteen total trials, 64 total tasks including warmups; 48 measured tasks.
- All tasks and worker identities passed validation against the independent sieve.
- Development image identity reported by Docker:
  sha256:92d7a03b9d411e542655be7206c08c4e9fddbc4da0c5aade226446d799067859.
  The Dockerfile uses distribution packages, not a hermetically pinned toolchain.
- Full executable/workload hashes, environment, load samples, and configuration are
  retained in metadata.json and trials.jsonl. The hashes were rechecked after export,
  and summary.json was independently recomputed from all retained raw trials.

### Checkout dirty flag: retained, not rewritten

metadata.json records git_dirty=true. Windows Git reported a clean checkout before
and after the measurement; Linux Git inside the read-only mount reported CRLF text
files as modified because it did not inherit Windows' global core.autocrlf=true.
The following post-run checks produced no differences/modified entries:

```sh
git -c safe.directory=/workspace -C /workspace diff --ignore-cr-at-eol --exit-code
git -c safe.directory=/workspace -c core.autocrlf=true -C /workspace status --porcelain
```

No source or report values were changed to conceal the flag. This is a disclosed
cross-platform provenance discrepancy; do not describe the report itself as
recording a clean checkout. The checkout SHA is not binary build attestation.
Future stronger experiments should use a native Linux checkout with explicit Git
configuration rather than relying on the host's global settings.

## Results

| Profile | Median four-task batch | Validated throughput | Task latency p50 | Task latency p95 |
|---|---:|---:|---:|---:|
| Direct sequential | 1.000 s | 4.005 tasks/s | 0.247 s | 0.260 s |
| Direct parallel, 2 processes | 0.496 s | 8.029 tasks/s | 0.244 s | 0.257 s |
| Forge, 1 worker process | 4.044 s | 0.993 tasks/s | 1.002 s | 1.030 s |
| Forge, 2 worker processes | 2.042 s | 1.960 tasks/s | 1.003 s | 1.032 s |

Each p50/p95 uses only 12 measured task samples, with nearest-rank quantiles. Those
are descriptive sample statistics, not strong population tail-latency evidence.
Throughput is total validated tasks divided by total measured batch time; it is
not derived by dividing the batch size by the median. Full precision and spread
are retained in summary.json.

Two-worker Forge took about 4.12 times as long as the matching direct-parallel
baseline and about 2.04 times as long as the sequential baseline. All answers
were correct, but this workload is not faster through Forge. Kernel compute
medians were around 0.21 seconds while Forge submission-to-result medians were
around 1 second. These separate observations expose overhead; they do not isolate
its cause or measure pure scheduler dispatch latency.

The worker's current 500-ms heartbeat/result polling design is a candidate to
profile, not a measured attribution of the entire difference. Phase instrumentation
and a controlled before/after experiment are needed before claiming an optimization.
Do not rerun with selectively favorable inputs and replace this baseline.

## Repeat the experiment

Build Release from the source commit, build forge-dev, then in PowerShell from
the repository directory (adjust its absolute path if needed):

```powershell
docker run -d --init --name forge-benchmark-repeat --cpus 2 --memory 2g --mount type=bind,source=C:\git\Forge,target=/workspace,readonly forge-dev sleep 900
docker exec forge-benchmark-repeat python3 /workspace/benchmarks/run.py --binary /workspace/build/release/forge --output /tmp/forge-results --environment-kind container --label "describe your actual host and background load" --purpose measurement --transport tls --workers 1 2 --tasks 4 --limit 500000 --step 10000 --repetitions 3 --warmups 1 --timeout 30
mkdir build\benchmarks -ErrorAction SilentlyContinue
docker cp forge-benchmark-repeat:/tmp/forge-results build\benchmarks\repeat-001
docker rm -f forge-benchmark-repeat
```

Use a new container and output destination for each run. Exact timings will vary.
The complete output, including individual streams/logs, was exported locally to
build/benchmarks/2026-11-03-local-001 before the original container was removed.
The three committed JSON files preserve the configuration, raw observations
(including base64 streams), and derived summary without private credentials.
