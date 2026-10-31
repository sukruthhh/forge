# Forge

Turn every computer you own into one private compute cluster.

Forge is a local-first, self-hosted distributed batch-compute runtime targeting
heterogeneous machines. Stack: **C++20 | Linux | TCP/IP | CMake**.

## Current status

Current milestone: resource-aware placement, a bounded/versioned TCP protocol,
coordinator, worker registration, heartbeats, and cluster inspection on Linux
loopback. Includes scheduler/protocol tests, TCP integration tests, and Linux CI.
Remote process execution is not implemented yet. No physical-cluster benchmark
results exist. See [protocol details and limits](docs/protocol.md).

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
```

Stop a worker and inspect the cluster again to see its DEAD record. Connections
are restricted to `127.0.0.1`; these commands do not join physical remote machines
yet. The worker currently reports system CPU/RAM totals without accounting for
container limits and advertises CUDA as unavailable.
