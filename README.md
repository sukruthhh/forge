# Forge

Turn every computer you own into one private compute cluster.

Forge is a local-first, self-hosted distributed batch-compute runtime targeting
heterogeneous machines. Stack: **C++20 | Linux | TCP/IP | CMake**.

## Current status

Current milestone: resource-aware placement, a bounded/versioned TCP protocol,
coordinator, worker registration, heartbeats, cluster inspection, and scheduled
command execution on Linux loopback. Includes scheduler/protocol tests, TCP and
execution integration tests, and Linux CI. No physical-cluster benchmark results
exist. See [protocol details](docs/protocol.md) and [execution limits](docs/execution.md).

See [the canonical framework](docs/canonical-framework.md) and
[architecture decisions](docs/architecture.md).

For explanations, tradeoffs, implementation status, and practice questions, see
the [interview reference](docs/interview-reference.md).

## Build on Linux

Requires a C++20 compiler and CMake 3.20+.

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
are restricted to `127.0.0.1`; these commands do not join physical remote machines
yet. The worker currently reports system CPU/RAM totals without accounting for
container limits and advertises CUDA as unavailable.

`run` executes trusted commands already installed on a worker. Captured output is
limited to 16 KiB per stream, and runtime to 30 seconds. CPU/RAM reservations are
not OS-enforced limits. Authentication, file staging, sandboxing, batch mapping,
job history, and automatic retries are not implemented yet.
