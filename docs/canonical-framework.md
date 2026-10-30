# Forge canonical framework

Forge is a local-first, self-hosted distributed compute runtime that turns heterogeneous computers you already own into one private programmable batch-compute cluster.

Pitch: Turn every computer you own into one private compute cluster.

The authoritative planning transcript is canonical-framework-source.txt in this directory. The user removed the three-day deadline; its days represent implementation milestones, not deadlines.

## Product contract

- Simple CLI: forge join, forge cluster, forge run, forge map, forge ps, forge logs.
- Coordinator and workers communicate through a custom TCP protocol.
- Workers advertise OS, architecture, CPU, RAM, and GPU capabilities.
- Support round-robin, least-loaded, and resource-aware scheduling with compatibility checks.
- Execute remote processes and aggregate stdout, stderr, logs, and results.
- Detect worker failures through heartbeats and automatically reschedule eligible jobs.
- Add Linux namespaces and cgroups v2 isolation and CPU/RAM enforcement.
- Grow toward a desktop, laptop, and M3 Mac compute pool, including CUDA placement on the RTX 4070 machine. Linux isolation must not be claimed on macOS.

## Delivery milestones

1. Working CLI, coordinator, worker, TCP registration, heartbeats, capabilities, remote execution, and returned output.
2. Multiple workers, scheduling, failure recovery, cluster/job/log inspection, and parallel batch mapping.
3. Real workloads and benchmarks across one, two, and three physical workers; throughput, completion time, p50/p95 latency, utilization, failure detection, and recovery. Publish a clear README and reproducible demo.

Isolation and broader platform/GPU support are part of the target; their precise delivery order must be documented as architecture develops.

## Engineering standard

Use C++20 and CMake as recovered from the earlier planning chat. Keep major decisions explicit and explain their tradeoffs to the user. Keep the code interview-defensible: clear ownership, explicit job states and retry semantics, bounded protocol messages, and meaningful validation of failure behavior.

Distinguish implemented capabilities, planned capabilities, local simulation, and physical-machine evidence. Benchmark numbers must come from actual runs. Favor a useful developer experience and systems depth over unnecessary dependencies. Do not add unrelated web UI or infrastructure without a concrete product need.
