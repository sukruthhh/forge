# Forge

Turn every computer you own into one private compute cluster.

Forge is a local-first, self-hosted distributed batch-compute runtime targeting
heterogeneous machines. Stack: **C++20 | Linux | TCP/IP | CMake**.

## Current status

Initial foundation: resource-aware placement with compatibility checks, a minimal
CLI, CMake build, scheduler tests, and Linux CI. Networking and remote process
execution are not implemented yet. No physical-cluster benchmark results exist.

See [the canonical framework](docs/canonical-framework.md) and
[architecture decisions](docs/architecture.md).

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
