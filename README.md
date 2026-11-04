# Forge

Turn every computer you own into one private compute cluster.

Forge is a local-first, self-hosted distributed batch-compute runtime targeting
heterogeneous machines. 

Stack: **C++20 | Linux | TCP/IP | CMake**.

## Current status

Current milestone: resource-aware placement, a bounded/versioned TCP protocol,
coordinator, worker registration, heartbeats, cluster inspection, and scheduled
command execution, job inspection, retained logs, and mutual TLS for configurable
IPv4 endpoints. Plaintext development is limited to loopback; a shared token is
an optional additional gate. Includes scheduler/protocol tests, TCP, TLS, and
execution integration tests, and Linux CI. No physical-cluster benchmark results
exist. See [protocol details](docs/protocol.md) and [execution limits](docs/execution.md).

See [the canonical framework](docs/canonical-framework.md) and
[architecture decisions](docs/architecture.md).

For explanations, tradeoffs, implementation status, and practice questions, see
the [interview reference](docs/interview-reference.md).

## Build on Linux

Requires a C++20 compiler, CMake 3.20+, and OpenSSL 3 development libraries.
Tests also require Python 3 and the openssl executable. On Ubuntu:

```sh
sudo apt-get install build-essential cmake ninja-build libssl-dev openssl python3
```

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build --parallel
ctest --test-dir build --output-on-failure
./build/forge --help
```

## Linux development container

On a computer running Docker with Linux containers, from the repository directory:

```sh
docker build -f Dockerfile.dev -t forge-dev .
docker run --rm -v "${PWD}:/workspace" forge-dev sh -c 'cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure'
```

This container supplies build tools. It does not validate worker sandboxing or
physical-machine scaling.

## Try the registration milestone

Run these in three terminals in the same Linux environment (or inside the same
running development container):

```sh
./build/forge coordinator --port 9000
./build/forge join --id desktop --port 9000
./build/forge cluster --port 9000
./build/forge run --port 9000 --timeout 5 -- /usr/bin/printf 'hello from Forge\n'
```

Stop a worker and inspect the cluster again to see its DEAD record. Connections
default to `127.0.0.1`. To configure encrypted network endpoints, use the
[TLS setup and separate-machine checklist](docs/network-setup.md). Network behavior
has local-process and Docker-bridge tests; physical-machine validation is pending.
The worker currently reports system CPU/RAM totals without accounting for
container limits and advertises CUDA as unavailable.

`run` executes trusted commands, with optional bounded flat-file staging. Captured output is
limited to 16 KiB per stream, and runtime to 30 seconds. CPU/RAM reservations are
not OS-enforced limits. General dependency packaging, sandboxing,
persistent history and automatic retries are not implemented yet. Optional additional
authentication uses `FORGE_TOKEN`; see [authentication](docs/authentication.md)
and the [recovery audit](docs/recovery-audit.md).

The default `run` architecture is the submitting Linux machine's architecture.
Use `--arch aarch64` or `--arch x86_64` to target a worker explicitly. For commands
whose installed programs and dependencies support all eligible Linux workers,
`--arch any` allows placement on any architecture. This does not translate binaries
or install dependencies. See [execution](docs/execution.md) for the exact contract.

## Stage and map inputs

With a coordinator and staging-capable workers running, from the repository:

```sh
./build/forge map --arch any --file examples/map/summarize.py \
  --output ./map-results examples/map/inputs/*.json -- python3 summarize.py '{}'
```

This stages a shared Python script and one JSON input per task, runs compatible
tasks in parallel, and writes indexed stdout/stderr plus results.jsonl. Workers
need Python 3 installed. The sample data demonstrates the workflow; it is not a
performance benchmark. Use --host with the existing TLS environment for remote
endpoints. See [batch workloads](docs/batch-workloads.md) for limits and semantics:
16 flat files / 32 KiB per task, 256 inputs per batch, up to 16 in-flight tasks,
no automatic retries, and a new output directory for each invocation.

## Test a networked cluster locally

After building the Linux binary in build/recovery and building forge-dev:

```sh
python scripts/test-container-cluster.py
```

This starts four temporary containers on a private bridge, verifies mutual TLS,
placement on two workers, staged map from CLI-only source files, output inspection,
workspace cleanup, and worker loss, then cleans up.
It is a network integration test on one host, not a physical-cluster benchmark.

## Measure batch execution

The [benchmark harness](benchmarks/README.md) compares sequential execution,
matching local-parallel subprocesses, and Forge worker counts using a deterministic
synthetic CPU workload with an independent correctness oracle. It retains raw
samples and failures, records provenance, and suppresses speedups for invalid runs.
Use a Release build and follow the guide's environment and timing definitions.
CI smoke timings are not performance claims or physical-machine evidence.

## Inspect jobs after execution

Use ./build/forge ps and ./build/forge logs job-1 against the same coordinator.
The latest 64 completed jobs are retained in memory until eviction or restart.
See [job inspection](docs/job-inspection.md) for states and retrieval semantics.

## Background build watcher

An optional forge-build-watch Docker container can run scripts/watch.sh against this folder.
It watches source/test/build-configuration contents, builds in build/watch, and
runs CTest only after a change. It stays quiet while files are unchanged.

- View output: docker logs --tail 60 forge-build-watch
- Stop: docker stop forge-build-watch
- Resume: docker start forge-build-watch

Docker must remain running. This watcher does not implement Forge features or
continue AI development; it compiles and tests local changes. To create it on
another machine after building forge-dev, substitute its absolute repository path:

    docker run -d --init --name forge-build-watch --mount type=bind,source=ABSOLUTE_REPO_PATH,target=/workspace forge-dev sh scripts/watch.sh

The watcher does not replace the existing demo build directory or restart your
running coordinator/workers. Restart those processes after rebuilding their binary
to use new protocol features such as ps/logs.
