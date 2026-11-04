# Forge interview reference

Updated: November 3, 2026

Implementation baseline: Linux runtime with mutual TLS, IPv4 endpoints, and optional token admission (November 3)

Stack: **C++20 | Linux | TCP/IP | CMake**

This is a living reference, not a claim that the entire product is complete.
Update the status and evidence sections after each milestone. Design proposals
below describe intended behavior until backed by implementation and tests.

## 1. The explanation to lead with

**One sentence:** Forge is a local-first, self-hosted distributed batch-compute
runtime intended to turn heterogeneous computers you already own into one private
programmable compute cluster.

**Product pitch:** Turn every computer you own into one private compute cluster.

**Thirty-second answer, accurate today:**

> I'm building Forge in C++20 to distribute independent batch jobs across personal
> machines. The goal is a simple CLI backed by a coordinator that matches jobs to
> compatible workers and recovers eligible jobs after worker failure. So far I've
> established the CMake build and implemented and tested the resource-aware
> placement core, then added a Linux loopback TCP coordinator, worker registration,
> heartbeats, and cluster inspection. The first execution path now schedules
> explicit commands on workers, collects output and exit status, and handles
> runtime limits and worker loss. Optional shared-token authentication gates local
> sessions. I then added mutual TLS and explicit IPv4 endpoints, with network tests
> across separate containers. Physical-machine validation and retries are next.

**Longer explanation:** The target user owns several machines with different
resources but otherwise submits work to each machine manually. Forge should make
batch processing one workflow: submit inputs, match compatible hardware, reserve
resources, execute work, collect results, and inspect failures. Good initial
workloads include independent simulations, parameter sweeps, and data processing.
The project does not make a single arbitrary application automatically parallel.

**Why build it?** Personal heterogeneous compute is the product wedge. The
engineering learning goal is to connect networking, scheduling, process lifecycle,
resource control, and failure semantics in a usable system. Explain your own
motivation in your own words rather than presenting this document as a script.

## 2. What exists versus what is planned

| Capability | Current evidence |
|---|---|
| C++20 library and CLI built with CMake | Compiled in an Ubuntu 24.04 development container using GCC 13.3 and Ninja |
| CLI | help, version, coordinator, join, cluster, run, ps, logs; version 0.1.0-dev |
| Worker/job resource data models | `include/forge/scheduler.hpp` |
| Compatibility and free-resource checks | `src/scheduler.cpp`, covered by scheduler tests |
| Resource-aware placement heuristic | Implemented and tested, including deterministic ties |
| Test runner | Scheduler, protocol, and real TCP/subprocess integration suites; see validation record below |
| GitHub Actions | Workflow configured; its remote execution has not been verified in this reference |
| Coordinator, worker agents, TCP protocol | Registration, liveness, inspection, submission, assignment, and results; loopback and Docker bridge tests |
| Automatic capability detection | Worker reports Linux architecture and system CPU/RAM totals; no cgroup adjustment or GPU discovery |
| Round-robin and least-loaded policies | Planned; only the resource-aware heuristic exists |
| Worker process execution | Implemented: explicit argv, output/exit capture, runtime/output bounds, one job per worker |
| Job inspection and retained logs | Implemented: active jobs plus 64 finalized results in memory, explicit terminal states |
| File transfer, batch map, durable logs/history | Planned |
| Heartbeats | Implemented with READY/SUSPECT/DEAD inspection and session deadlines |
| Authentication and encryption | Mutual TLS 1.3 for IPv4 endpoints; optional FORGE_TOKEN gate; no per-worker identity binding or roles |
| Explicit workload architecture | run defaults to native; exact labels and explicit any override; heterogeneous placement tested with simulated advertisements |
| Retries and attempt tracking | Planned |
| Namespaces and cgroups v2 | Planned |
| Native macOS workers and CUDA placement | Planned; current matching only checks a supplied CUDA boolean |
| Physical-machine benchmarks and external users | No results or user evidence yet |

**Validation record, October 31:** GCC 13.3 Linux Debug build succeeded in the
Ubuntu development container. The initial execution run passed 4/4 CTest suites
(scheduler, protocol, TCP integration, execution integration), taking about 28.7
seconds in total. The final build after PID-safe process cleanup passed all four
suites in about 32.1 seconds, including the added descendant-pipe timeout and
two-worker placement checks.
Network integration includes 120 cluster queries using eight concurrent clients.
These are correctness checks, not throughput or latency benchmarks. Remote
GitHub Actions results remain unverified here.

Do not describe CUDA flags as GPU discovery, resource checks as enforced limits,
or a container test as a benchmark across physical machines. The live registry
is now wired into scheduler reservations and execution. Reservations remain
placement accounting, not kernel resource enforcement.

## 3. Intended user experience

join, cluster, run, ps, and logs support default loopback or TLS IPv4 endpoints. Other commands below
remain the target interface. Current join requires `--id NAME`, and run requires
options before `--` followed by command arguments:

```sh
forge join
forge cluster
forge run ./benchmark --cpu 4
forge map inputs/*.json -- python analyze.py {}
forge ps
forge logs <job>
```

Current runnable example: `forge run --timeout 5 -- /usr/bin/printf hello`.
Files and dependencies must already exist on the worker; no upload is performed.

`run` submits one job; `map` creates independent jobs from a collection of inputs.
The CLI syntax and packaging rules still need implementation decisions. For
example, an executable built for Linux x86_64 cannot simply run on macOS ARM64.
An interpreter-based workload also needs compatible interpreters and dependencies.

## 4. Architecture and ownership

Intended path:

```text
CLI -> coordinator -> compatible worker -> child process
          ^                  |
          +-- status, output, completion --+
```

- **CLI:** submits requests and presents cluster, job, and log information.
- **Coordinator:** owns the queue, worker registry, reservations, assignments,
  job/attempt state, and scheduling decisions.
- **Worker:** reports capabilities and liveness; manages child processes, output,
  cancellation, and eventual resource enforcement.
- **Scheduler core:** pure eligibility and placement functions. It does not open
  sockets, launch processes, persist state, or reserve resources itself.
- **Execution backend:** platform-specific process and isolation operations.

Start with one coordinator to keep ownership and failure behavior understandable.
It is a single point of failure. Worker-failure recovery does not imply coordinator
restart recovery; durable state and reconciliation are separate future work.

Keep the scheduler portable while targeting Linux execution first. A future
macOS backend needs different process/resource controls. Docker currently supplies
development tools on the Windows host; it is not Forge's implemented job sandbox.

## 5. The implemented scheduler: explain this precisely

Each worker has an ID, OS, architecture, CPU/RAM capacity, CPU/RAM reservations,
CUDA capability flag, and readiness flag. Each job specifies optional OS and
architecture constraints, CPU/RAM requirements, and whether CUDA is required.

### Eligibility

Reject a placement if:

1. Requested CPU is zero.
2. The worker is unavailable.
3. A specified OS or architecture does not match.
4. CUDA is required but absent.
5. Existing reservations exceed advertised capacity.
6. Requested CPU or RAM exceeds the remaining capacity.

Empty OS/architecture constraints currently mean unrestricted matching. Correct
job submission must eventually derive or validate constraints for the artifact.
Memory request zero currently means no RAM reservation, not unlimited enforced
memory. Those are model semantics, not a completed execution policy.

Capacity is checked before subtraction to avoid unsigned underflow. Fit is checked
before adding requests to reservations, preventing overflow in the projected
resource totals. Worker CPU capacity zero cannot pass a positive CPU request.

### Placement score

For every eligible worker:

```text
projected CPU fraction = (reserved CPU + requested CPU) / CPU capacity
projected RAM fraction = (reserved RAM + requested RAM) / RAM capacity
score = max(projected CPU fraction, projected RAM fraction)
```

Choose the lowest score; break equal scores by lexicographically smaller worker
ID. If RAM capacity and requested/reserved RAM are all zero, its RAM fraction is
defined as zero. No eligible worker produces an empty optional, not a crash.

**Example:** A job needs 4 CPU units and 8 GiB. An idle 8-CPU/16-GiB worker scores
0.5; an idle 16-CPU/32-GiB worker scores 0.25. Choose the larger worker if both meet
the compatibility constraints.

**Complexity:** O(W) worker visits and O(1) auxiliary space for each selection,
aside from temporary rejection messages. String comparisons depend on string
length. Avoid claiming constant-time scheduling or a measured throughput figure.

**Tradeoffs:** Simple and testable, but it does not predict runtime, account for
CPU model differences, data locality, GPU memory, fairness, or starvation. Stable
tie-breaking can favor the same ID. This is a projected utilization heuristic,
not dominant-resource fairness and not a proof of globally optimal scheduling.

**Critical boundary:** Selection does not mutate workers. The coordinator now
holds the registry mutex across selection and reservation. It admits one job per
worker; completion or session loss releases the reservation once.

## 6. Networking questions to prepare for

The application protocol is implemented with Linux sockets and poll-based I/O
deadlines, without Boost.Asio. TCP offers an ordered byte stream, not application messages.
A receive can contain part of a message or multiple messages. A successful send
does not prove that a worker executed the job or persisted the assignment.

Networking now links OpenSSL 3 for mutually authenticated TLS; plain Linux socket
I/O remains available for loopback development. See [network setup](network-setup.md).
The protocol has versioning, explicit lengths, maximum frame sizes, message
validation, partial-read/write handling, and absolute monotonic I/O deadlines.
Version 1 uses binary length-prefixed fields rather than the earlier JSON proposal.
See [the exact wire format](protocol.md). Registry access uses a mutex and handler
threads have fixed slots. No networking occurs while holding the registry lock.
Input/output transfers need their own limits and backpressure so one slow client
or noisy process cannot consume unbounded memory.

**Why TCP?** It provides a useful reliable stream abstraction while leaving the
application's framing, state transitions, and failure behavior explicit.

**Why C++?** Direct integration with OS APIs and explicit resource lifetime control
fit the systems focus. RAII can own sockets, descriptors, and child-process
resources. C++ also requires careful lifetime and concurrency management; choosing
it alone does not demonstrate lower latency or greater reliability.

**Why CMake?** It describes targets and dependencies and integrates the build with
CTest. Ninja is a build executor; GCC is the compiler. These are different roles.

## 7. Failure detection implemented; job recovery planned

Workers send heartbeats about every 500 ms. Snapshot inspection marks one second
without contact SUSPECT and two seconds DEAD; disconnects also mark DEAD. The
registered session has a two-second receive deadline. This is inspection/session
cleanup, not a rescheduling loop. Duplicate connected IDs are rejected; a closed
session's ID can reconnect. There is no automatic worker reconnect yet.

Current active states are QUEUED, ASSIGNED, RUNNING. Terminal states are SUCCEEDED,
FAILED, TIMED_OUT, LOST, UNSCHEDULABLE, and CANCELLED. A terminal response carries
exit status or an infrastructure error. RUNNING means assignment is being sent,
not an acknowledged successful exec. There are no retries, attempt IDs, durable
history, or cancellation API yet. Completed results are retained in memory for 64 jobs. Worker loss fails the job without reassignment.

Heartbeats provide evidence of recent contact. A missed heartbeat may indicate a
network partition or overload rather than process death. Use monotonic elapsed
time for local timeouts and distinguish suspicion from declaring a worker lost.
Shorter timeouts detect some failures sooner but increase false suspicions.

In the future, eligible lost-worker jobs may be requeued with a bounded retry budget.
The coordinator must reject late results from superseded attempts and release
reservations exactly once for the accepted transition. Late or duplicate messages
must not double-release resources or overwrite a newer result.

**Does this guarantee exactly-once execution?** No. A partitioned worker may keep
running while another attempt starts. Attempt IDs protect coordinator bookkeeping
but cannot undo repeated external side effects. Initially target retry-safe jobs;
external writes need idempotency or stronger application-level coordination.

**Does a failed command always get retried?** No retries exist yet. Future policy must separate
infrastructure loss from a deterministic application error. Infinite retries would
hide bugs and waste resources. Cancellation must also prevent queued retries and
eventually terminate the relevant child process tree.

## 8. Execution implemented; isolation and security planned

Workers launch explicit argv using posix_spawnp, inheriting the worker's environment
and working directory. Stdin is /dev/null. Separate nonblocking-drained pipes
capture stdout/stderr, retaining 16 KiB per stream while discarding excess output
to avoid deadlock. Timeout is 1..30 seconds; it kills the process group and returns
124. Spawn errors return 127; signals return 128+signal. An explicit timeout flag
distinguishes timeout from a program choosing the same exit code. Heartbeats run
while execution proceeds on a separate asynchronous task. See [execution.md](execution.md).

No file staging, environment filtering, user-selected working directory, or live
log streaming exists yet. Graceful worker cleanup kills group descendants, but
SIGKILL cannot run cleanup and children may survive. Processes deliberately
escaping their group are outside the trusted-workload scope.

Namespaces isolate selected views of system resources; cgroups v2 account for and
control resources such as CPU and RAM. CPU bandwidth limits are not identical to
CPU affinity. Scheduling reservations alone enforce neither. Child descendants
must be included in lifecycle and resource management.

Neither feature by itself establishes a complete hostile-code sandbox. Initial
scope is trusted workloads on a private cluster. Authenticate submissions and
worker membership before exposing remote command execution beyond localhost;
define transport protection and credential handling before wider deployment.
Optional shared-token authentication gates coordinator connections when FORGE_TOKEN
is configured. Mutual TLS adds encryption, CA verification, and coordinator IP
verification. There is no role separation, certificate revocation checking, or
binding of worker IDs to certificate identities. The token is inherited by trusted workloads;
see [authentication](authentication.md).

Packaging needs size limits, path traversal protection, explicit dependencies,
and compatible artifacts. A CUDA flag alone is not proof of driver/runtime
compatibility, sufficient VRAM, or exclusive GPU availability.

## 9. Testing and evidence

Verified foundation checks cover compatible placement, OS/architecture mismatch,
CPU/RAM reservations, invalid reservations, CUDA requirements, unavailable workers,
projected-utilization preference, stable ties, zero CPU, oversized RAM requests,
and an empty cluster. This is unit-level scheduler coverage, separate from the TCP
integration coverage. The tests use explicit checks that remain active in release
builds rather than relying on C++ `assert`.

The networking milestone adds protocol boundary/round-trip checks and real Linux
subprocess/TCP integration checks: two workers, continued heartbeats, duplicate
IDs, fragmented/coalesced messages, malformed framing, partial-header deadlines,
concurrent inspection/handler reuse, worker death, ID reuse, and shutdown.
This remains local-container evidence, not a physical network or job-recovery demo.

Execution integration checks literal shell syntax/argument boundaries, separate
output streams, nonzero exits, spawn failure, large and binary output, live
heartbeats during a job, runtime and descendant-pipe deadlines, serial reservation
ownership, impossible resource requests, concurrent placement on two different
worker PIDs, worker loss, and replacement capacity.

Future execution validation should cover disconnects during dispatch, stale completions,
job recovery after worker loss, retry exhaustion, cancellation, and coordinator restart
once persistence is implemented. Fault injection should produce reproducible
results and verify invariants, not just a visually convincing demo.

### Benchmark plan; no results yet

Use identical workloads and report one-, two-, and three-physical-worker runs.
Record hardware, OS, compiler/build mode, input size, job count, repetitions,
resource requests, and whether staging/startup time is included.

| Metric | Definition |
|---|---|
| Batch completion time | Elapsed time from a declared submission boundary to all required results |
| Throughput | Completed jobs divided by measured elapsed seconds |
| Dispatch latency | Time from enqueue to a defined dispatch/assignment event |
| End-to-end job latency | Submission to terminal result; distinct from dispatch latency |
| p50/p95 | Median/95th percentile of the stated latency samples |
| Speedup | Single-worker completion time divided by multi-worker completion time |
| Completion-time reduction | `(T_single - T_multi) / T_single * 100%` |
| Failure detection | Controlled failure to coordinator's loss declaration |
| Recovery | Controlled failure to a defined event: reassignment or recovered completion; label which |
| Recovery success | Successfully recovered eligible jobs divided by eligible interrupted jobs |

Reservations are not measured CPU utilization. Cross-machine timestamp subtraction
needs clock synchronization or a measurement design avoiding clock-offset error.
Do not confuse three containers on one computer with three physical machines.
Heterogeneous machines need meaningful baselines; dividing speedup by worker count
does not establish fair efficiency when workers have different capacities.

Expect overhead from dispatch, transfers, startup, and collection. Very short jobs
may scale poorly. Report repeated measurements and variation. Poor scaling is a
finding to investigate, not a reason to manufacture stronger numbers.

## 10. Likely interview questions: short answers

**What is the hardest completed part so far?** Discuss either resource-accounting
edge cases or TCP framing, deadlines, and session ownership. Do not cite automatic
job recovery as completed yet.

**Why not Kubernetes or a large distributed framework?** Forge targets a narrower
personal batch-compute workflow and makes scheduling/execution behavior explicit
for learning and usability. It does not currently match mature orchestration
systems in availability, ecosystem, or security capabilities.

**What happens when nothing fits?** The scheduler returns no placement. The
coordinator queues the job for up to five seconds, then returns an error if no
placement becomes available. It does not distinguish permanently impossible
requests from temporarily unavailable capacity yet.

**What is least loaded?** The definition must be explicit: reservation fractions,
active job count, and observed CPU use are different signals. Current scheduling
uses projected CPU/RAM reservation fractions, not sampled OS load.

**Why not Raft immediately?** Replication introduces another state machine and
failure model. First establish correct single-coordinator behavior and measure
the need. The availability limitation remains explicit.

**What would you improve next?** Durable job history, input staging,
batch mapping, authenticated cross-machine connections, and explicit retry
semantics. Prioritize these against real workloads and measured bottlenecks.

**How did you use AI?** Be direct: AI assisted implementation, documentation, and
iteration. Explain what you reviewed, tested, changed, and can demonstrate yourself.
Do not claim independent authorship of generated code. Before presenting a module,
be able to walk through its control flow, invariants, tradeoffs, and failure cases.

**What is the biggest limitation today?** Execution works between local worker
processes with preinstalled programs/files, tested locally and over a Docker bridge. There is no verified
physical cluster, upload, sandboxing, automatic recovery, or real scaling evidence.

## 11. Resume targets: future XYZ bullets

Use only after implementation and recorded measurements. Replace every placeholder.

- Reduced batch completion time by **[X%]** across **[N] physical machines** by
  building a C++ distributed runtime with resource-aware scheduling and parallel
  job execution.
- Recovered **[X%] of eligible interrupted jobs within [Y seconds]** across **[N]
  failure trials** by implementing heartbeats, failure detection, and automatic
  reassignment. State whether Y measures reassignment or recovered completion.
- Achieved **[X jobs/sec] with [Y ms] p95 dispatch latency** on **[specified workload
  and cluster]** by implementing a custom TCP coordinator-worker protocol and
  asynchronous dispatch, once those features exist and their effect is supported.

Do not attribute an improvement to a feature without a relevant baseline. Keep
benchmark commands, raw results, and configuration with the eventual claims.

## 12. Before every interview

1. Refresh this document against the exact commit you plan to demonstrate.
2. Practice the thirty-second explanation and one detailed scheduler walkthrough.
3. Identify one real bug, its cause, the fix, and the test that prevents recurrence.
   No bug story has been recorded yet; do not invent one.
4. Prepare an actual demo with a fallback recording once distributed execution works.
5. Bring measured results and limitations; keep planned capabilities in future tense.

### Code and source map

- [Canonical framework](canonical-framework.md)
- [Original planning transcript](canonical-framework-source.txt)
- [Architecture decisions](architecture.md)
- [Scheduler model/API](../include/forge/scheduler.hpp)
- [Scheduler implementation](../src/scheduler.cpp)
- [Scheduler tests](../tests/scheduler_tests.cpp)
- [CLI](../src/cli.cpp)
- [Build configuration](../CMakeLists.txt)
- [CI configuration](../.github/workflows/ci.yml)
- [Wire protocol and session limits](protocol.md)
- [Networking implementation](../src/network.cpp)
- [Coordinator and worker sessions](../src/runtime.cpp)
- [TCP integration checks](../tests/network_integration.py)
- [Execution lifecycle and protocol](execution.md)
- [Process execution backend](../src/executor.cpp)
- [Execution integration checks](../tests/execution_integration.py)

Maintenance rule: when code changes, update implemented status, design decisions,
validation evidence, limitations, and the short pitch together. The canonical
framework remains the product target; this reference tracks defensible claims.

## Job inspection milestone (November 1)

ps shows active and retained jobs with assigned worker, terminal state, and exit
status. logs retrieves the last accepted stdout/stderr and timeout/truncation
flags. Fetching a failed command's logs succeeds as an inspection operation;
it does not repeat the command or return its original failure as retrieval status.
Infrastructure loss still returns an explanatory error.

History retains 64 finalized results, evicts the oldest finalized entry, and is
lost on restart. Live streaming is not implemented. The protocol caps and bounded
command summaries keep all active/history rows within one frame. Retention tests
use a deterministic worker stub to finish 70 jobs quickly; this is coordinator
correctness evidence, not execution throughput evidence. Real-process execution
tests cover binary log retrieval, RUNNING inspection, timeouts, and worker loss.

See [job inspection semantics](job-inspection.md) and
[retention integration tests](../tests/history_integration.py).
Validation for this milestone: the initial five-suite Linux CTest run passed
(scheduler, protocol, network, execution, history). The history test completed
70 stub jobs and verified the 64-entry bound, eviction, and failed-result retrieval.
After adding live state, binary log, timeout log, and lost-worker inspection
assertions, the affected real-process execution suite passed again in about
24.6 seconds. No physical-cluster performance claims follow from these checks.

## Recovery and authentication foundation (November 3)

Recovered main at 2cd3c49 with a clean checkout matching origin/main after fetch.
Started Docker Desktop and rebuilt Dockerfile.dev on this laptop. A fresh Ubuntu
24.04/GCC 13.3 Debug build passed all five baseline CTest suites in 32.92 seconds.
See the [recovery audit](recovery-audit.md) for implemented/partial/missing features.

Added optional FORGE_TOKEN authentication before every coordinator operation.
All six suites passed in the final Debug run (36.57 seconds), including rejected
credentials, malformed and repeated authentication, rejected registration without
registry mutation, coalesced handshake/request frames, an authenticated request
deadline, successful worker execution and ps/logs, and invalid configuration.
These durations describe correctness runs, not benchmarks.

The shared credential authorizes all operations and is sent unencrypted over
loopback. It is not server authentication, independent worker identity, a sandbox,
or evidence of physical-machine execution. Keep loopback binding. The next
Milestone A work is authenticated encrypted transport, remote endpoint policy,
and separate-machine validation. GitHub Actions status could not be retrieved
through the connected GitHub tool during this recovery; local validation is the
evidence recorded here.

## Mutual TLS and network endpoints (November 3)

Implemented --bind for coordinators and --host for worker/client commands with
numeric IPv4 endpoints. Non-loopback use requires mutual TLS 1.3 via OpenSSL 3.
Clients verify the coordinator IP SAN and CA chain; coordinators require a trusted
client certificate. TLS configuration is explicit and fails closed, with no
plaintext fallback or verification bypass. Optional token admission runs inside
TLS. See [network setup](network-setup.md) for exact commands and limits.

Connection owns a socket and SSL object. Nonblocking TLS retries preserve an
absolute frame deadline; handshakes have a separate three-second bound and run
inside existing handler slots. The context outlives all coordinator handlers.
The CLI ignores SIGPIPE for transport safety; child spawn attributes reset SIGPIPE
to its normal disposition. A regression test confirms child signal exit 141.
Encrypted key files are rejected without an interactive prompt.

Final local validation: Ubuntu 24.04, GCC 13.3 Debug, OpenSSL 3.0.13; all seven
CTest suites passed in 54.50 seconds. TLS coverage includes missing/untrusted or
expired client certificates, wrong server trust/IP, certificate purpose, TLS 1.2
rejection, partial-frame and client/server handshake deadlines, binary output and
limits, execution timeout, retained logs, token admission, active-job worker loss,
and remaining capacity. These durations are correctness-run timings.

The final binary also passed scripts/test-container-cluster.py: coordinator,
two workers, and CLI in four containers on one private Docker bridge. Concurrent
jobs returned both expected worker container hostnames; ps/logs, plaintext rejection,
worker-container loss, and subsequent execution on the remaining worker passed.
The script removes its temporary containers and network. CI now includes OpenSSL
installation, all CTest suites, and this Docker network test; remote CI results
are not established by local runs.

No physical-machine execution or scaling result is claimed. TLS certificates grant
all operations; worker IDs are not bound to certificate subjects. Certificate
revocation/rotation automation, roles, safer workload credentials, isolation,
staging/map, and retries remain open. The concrete next validation step is the
separate-Linux-machine checklist in network-setup.md, followed by workload staging.

## Explicit workload architecture (November 3)

forge run now accepts --arch native (the default), --arch ARCH for an exact
worker-advertised label, and --arch any for an explicitly portable command.
Native resolves to the submitting Linux system's uname machine label; any sends
an empty architecture constraint to the existing placement library. Linux OS,
resources, liveness, and single-job worker eligibility are still enforced.
Empty/malformed CLI values and repeated options are rejected. Valid labels with
no matching capacity expire under the existing five-second placement deadline.

Why keep native as the default? An arbitrary executable can be machine-specific.
Why expose any? A preinstalled interpreter/command with compatible dependencies
can be available on both x86_64 and aarch64 workers. Forge cannot infer that
portability merely from argv. The caller remains responsible until artifact
packaging provides a stronger compatibility contract. This feature adds neither
emulation nor dependency installation. See [execution](execution.md).

Validation: all eight CTest suites passed in the Ubuntu/GCC 13.3 Debug environment
(61.93 seconds). The new architecture suite uses two simulated worker advertisements
to verify default/native/exact/any placement, incompatible-target expiry, and input
validation. The real-process execution suite checks native and any on the local
architecture; these do not establish ARM64 execution. TLS integration also exercises
an any submission. Physical ARM64 verification remains pending the Mac's Linux VM.
The final TLS any case passed in a targeted rerun, and the four-container private
network test passed again with the updated binary.

The prior TLS commit 09d2666 also passed
[GitHub Actions](https://github.com/sukruthhh/forge/actions/runs/41711281090), including
the container network test; that remote evidence was confirmed after its local
validation record was written. Current full-scope milestones and deferred fleet
setup are tracked in [development status](development-status.md).
