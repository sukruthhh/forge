# Recovery audit: November 3, 2026

Baseline: `2cd3c49`, recovered on Windows at `C:\git\Forge`. The working tree
was clean. A successful fetch confirmed that local main and origin/main matched.
No existing watcher or build directory was assumed or recreated.

## Architecture and dependencies

C++20 and CMake build a portable placement library, a Linux runtime/network
library, and one CLI executable. The runtime uses Linux sockets, poll deadlines,
RAII descriptors, a fixed array of 96 connection-handler threads, and a
mutex-protected in-memory registry. The worker runs one asynchronous process
execution task while its main thread exchanges heartbeats and results.

Placement filters compatibility and unreserved resources, then minimizes the
projected maximum CPU/RAM reservation fraction with worker-ID tie breaking.
Selection and reservation occur under the same coordinator lock. Execution uses
posix_spawnp, process groups, nonblocking output pipes, and monotonic deadlines.
Completion or session loss releases the worker reservation. Live worker IDs
cannot be replaced, preventing stale-session cleanup from changing a replacement.

Dependencies are the C++ standard library, Linux/POSIX APIs, and Threads. Python 3
drives integration tests. There is no Boost, database, Catch2, or runtime Docker
dependency. Docker supplies Linux build tools on this Windows machine.

## Verification

Docker Desktop was installed but stopped. Starting it and building Dockerfile.dev
provided Ubuntu 24.04, GCC 13.3, CMake, Ninja, and Python 3.12.3. The WindowsApps
WSL launcher failed; a separate WSL distribution was not needed for this build.

```sh
cmake -S . -B build/recovery -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build/recovery --parallel 2
ctest --test-dir build/recovery --output-on-failure
```

All five baseline suites passed: scheduler, protocol, network_integration,
execution_integration, and history_integration. Total test time was 32.92 seconds;
this is a correctness run, not a performance benchmark.

## Actual feature state

| State | Capabilities and limits |
|---|---|
| Verified locally | Registration, heartbeat liveness, cluster inspection, resource-aware placement, remote worker-process execution over loopback, separate binary-safe output, exit propagation, output/runtime limits, worker loss, ps/logs, 64-result retention |
| Partial | Capacity reports system totals rather than affinity/cgroup availability; reservations do not enforce limits; RUNNING means dispatch rather than acknowledged exec; queue expires after five seconds and is not FIFO |
| Missing at baseline | Authentication, encrypted transport, configurable remote endpoints, physical-machine execution evidence, staging/map, attempt IDs/retries, cancellation API, persistence, namespaces/cgroups, GPU discovery, native macOS backend, benchmarks |
| Broken during recovery | No baseline test failures. The stopped local Docker engine initially prevented compilation; starting it resolved the build prerequisite |

Review confirmed bounded protocol parsing, shared header/body I/O deadlines,
session-owned registration, lock-protected reservations, and PID-safe group
cleanup. This is source review and local test evidence, not a security evaluation.
Known risks remain: a killed worker can leave children running, submitted commands
inherit the worker environment and privileges, and coordinator restart loses state.

## Next increment

Add optional shared-token authentication before any coordinator operation, keeping
the listener on loopback. Test rejected registrations/submissions/inspection,
valid execution, malformed handshakes, deadlines, and credential configuration.
This is a prerequisite for Milestone A. Encrypted transport, credential lifecycle,
remote endpoint configuration, and tests on separate Linux machines remain open.

Implemented this increment and reran all six CTest suites successfully in the
same Debug environment (36.57 seconds). Authentication coverage includes invalid
credentials and schemas, admission before registry mutation, successful execution
and inspection, coalesced frames, and the post-authentication request deadline.
