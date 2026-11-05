# Forge interview reference

Updated: November 5, 2026

Implementation baseline: Linux runtime with TLS, staging/map, attempts/retries,
cancellation and delegated cgroup-v2 enforcement.

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
> across separate containers. Bounded worker-loss retries and cancellation are
> implemented; physical-machine validation is pending.
> Bounded file staging and map now distribute independent inputs and save per-task
> results. Visible affinity/cgroup discovery and delegated CPU/RAM enforcement
> are implemented; larger packaging and namespace isolation remain open.

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
| CLI | help/version, coordinator/join, cluster/details, run/map, ps/logs, timings, attempts, cancel; version 0.1.0-dev |
| Worker/job resource data models | `include/forge/scheduler.hpp` |
| Compatibility and free-resource checks | `src/scheduler.cpp`, covered by scheduler tests |
| Resource-aware placement heuristic | Implemented and tested, including deterministic ties |
| Test runner | Scheduler, protocol, and real TCP/subprocess integration suites; see validation record below |
| GitHub Actions | Debug/Release suites and TLS fixture verified on earlier milestone commits; new pushes require checking their own workflow |
| Coordinator, worker agents, TCP protocol | Registration, liveness, inspection, submission, assignment, and results; loopback and Docker bridge tests |
| Automatic capability detection | Linux architecture, visible CPU affinity/quota and memory headroom; registration snapshot, no GPU discovery |
| Round-robin and least-loaded policies | Planned; only the resource-aware heuristic exists |
| Worker process execution | Implemented: explicit argv, output/exit capture, runtime/output bounds, one job per worker |
| Job inspection and retained logs | Implemented: active jobs plus 64 finalized results in memory, explicit terminal states |
| File staging and batch map | Up to 16 flat files / 32 KiB per task; client-driven map, up to 256 inputs and 16 in-flight jobs; local-process and TLS container tests |
| Benchmark tooling | Synthetic CPU oracle, sequential/local-parallel/Forge profiles, repeated raw samples, provenance, failure invalidation; not physical-cluster performance evidence |
| General dependency packaging, output files, durable history | Planned |
| Heartbeats | Implemented with READY/SUSPECT/DEAD inspection and session deadlines |
| Authentication and encryption | Mutual TLS 1.3 for IPv4 endpoints; optional FORGE_TOKEN gate; no per-worker identity binding or roles |
| Explicit workload architecture | run defaults to native; exact labels and explicit any override; heterogeneous placement tested with simulated advertisements |
| Retries and attempt tracking | Explicit worker-loss retries 0..3, session-owned attempt fencing and bounded history |
| Cancellation | Queued/running cancellation, retry prevention and explicit cleanup uncertainty |
| cgroups v2 | Delegated CPU/RAM enforcement, confined startup probe and cgroup-wide cleanup; real-kernel fixture |
| Namespace isolation | Planned |
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
placement accounting unless a configured cgroup backend applies kernel caps.

## 3. Intended user experience

join, cluster, run, map, ps, and logs support default loopback or TLS IPv4 endpoints.
Current join requires `--id NAME`, and run/map require
options before `--` followed by command arguments:

```sh
forge join --id desktop
forge cluster
forge run --cpu 4 -- ./benchmark
forge map --file analyze.py inputs/*.json -- python3 analyze.py '{}'
forge ps
forge logs <job>
```

Current runnable example: `forge run --timeout 5 -- /usr/bin/printf hello`.
Without --file, commands and inputs must already exist on the worker. --file can
stage a small flat package; interpreters and system dependencies must be installed.

`run` submits one job; `map` creates independent jobs from a collection of inputs.
The implemented limits and results contract are in [batch workloads](batch-workloads.md).
Richer packaging and artifact compatibility remain open. For example, an executable
built for Linux x86_64 cannot simply run on macOS ARM64.
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
cleanup; opted-in jobs can be requeued on session loss. Duplicate connected IDs are rejected; a closed
session's ID can reconnect. There is no automatic worker reconnect yet.

Current active states are QUEUED, ASSIGNED, RUNNING. Terminal states are SUCCEEDED,
FAILED, TIMED_OUT, LOST, UNSCHEDULABLE, and CANCELLED. A terminal response carries
exit status or an infrastructure error. RUNNING means assignment is being sent,
not an acknowledged successful exec. Numbered attempts and opt-in bounded retries
are implemented; durable history remains open. Explicit cancellation is implemented
with cleanup evidence as described below. Completed
results are retained in memory for 64 jobs. By default worker loss fails without
reassignment; --retries 1..3 permits additional worker-loss attempts.

Heartbeats provide evidence of recent contact. A missed heartbeat may indicate a
network partition or overload rather than process death. Use monotonic elapsed
time for local timeouts and distinguish suspicion from declaring a worker lost.
Shorter timeouts detect some failures sooner but increase false suspicions.

Eligible lost-worker jobs now requeue within an explicit bounded retry budget.
The coordinator rejects stale attempt results and releases reservations under the
registry mutex. Late or duplicate messages cannot overwrite an accepted result.

**Does this guarantee exactly-once execution?** No. A partitioned worker may keep
running while another attempt starts. Attempt IDs protect coordinator bookkeeping
but cannot undo repeated external side effects. Initially target retry-safe jobs;
external writes need idempotency or stronger application-level coordination.

**Does a failed command always get retried?** No. Only opted-in worker-session loss
permits another attempt. Command failures, setup/launch errors and execution
timeouts stay terminal. Retry count is at most three additional attempts, and
coordinator shutdown prevents further dispatch. Explicit cancellation prevents
further attempts; stronger process-tree enforcement remains open.

## 8. Execution implemented; isolation and security planned

Workers launch explicit argv using posix_spawnp, inheriting the worker's environment.
Plain jobs use its working directory; staged jobs use a fresh temporary directory.
Stdin is /dev/null. Separate nonblocking-drained pipes
capture stdout/stderr, retaining 16 KiB per stream while discarding excess output
to avoid deadlock. Timeout is 1..30 seconds; it kills the process group and returns
124. Spawn errors return 127; signals return 128+signal. An explicit timeout flag
distinguishes timeout from a program choosing the same exit code. Heartbeats run
while execution proceeds on a separate asynchronous task. See [execution.md](execution.md).

Flat-file staging and map exist. Environment filtering, arbitrary user-selected
working directories, and live log streaming remain open. Graceful worker cleanup kills group descendants, but
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

**What would you improve next?** Physical-machine validation, larger workload
packaging, durable job history, isolation, and explicit retry
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
   The staging milestone below records an assignment-frame size defect found in
   review and the admission-boundary regression that now covers it.
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
revocation/rotation automation, roles, safer workload credentials and isolation
remain open. Staging/map and bounded opt-in retries were added in subsequent
milestones below. Physical validation still follows network-setup.md.

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

## Bounded staging and parallel map (November 3)

Implemented --file for run and map, with flat regular files captured before
submission, 16-file / 32-KiB limits, executable intent, and a new worker directory
per task. Changed only the child's cwd through spawn file actions, preserving the
worker's heartbeat thread environment. Validated paths and counts at client,
coordinator, and worker boundaries. Inputs are cleared before job-history retention.
Staging is capability-gated; legacy workers still run ordinary submissions.

Map snapshots 1-256 inputs and shared files, replaces standalone {} argv tokens,
and runs up to 16 in-flight jobs capped by an initial compatible idle-worker
snapshot. It saves indexed binary stdout/stderr and per-task JSON as results arrive,
then produces an input-ordered results.jsonl and summary table. Command failures,
timeouts, infrastructure errors, unknown outcomes, and result-storage failures
remain explicit. No jobs are automatically retried. It is a client pipeline, not
a durable coordinator-owned batch object. Full limits are in
[batch workloads](batch-workloads.md).

Review found that a submission fitting a frame can grow when its resource/OS
prefix is replaced by the assignment's job-ID prefix. A maximum-size submission
could therefore be admitted even though its dispatch frame was too large. The
coordinator and client now validate the eventual assignment using the maximum
job-ID length before admission/submission. A raw, valid boundary-size staged
submission must return ERROR without changing job history. This records a source
review finding and a regression check, not an observed pre-fix physical failure.

Validation: GCC 13.3 Ubuntu 24.04 Debug build passed all nine CTest suites
(101.47 seconds). Staging/map coverage includes binary data, maximum file/data
bounds, executable modes, permissions, unique directories, success/failure/timeout
cleanup, unsafe manifests, duplicate names, FIFOs/symlinks, legacy-worker gating,
parallel worker placement, ordered input/result association, retained logs, mixed
exits, truncation, active-job worker loss, and single-worker fan-out capping.
RLIMIT_FSIZE failure injection verifies storage errors stop subsequent submissions
and do not trigger retries. Tests are correctness evidence, not benchmark results.

The updated four-container bridge test passed with source files created only in
the CLI container. Staged map tasks ran on both worker containers over mutual TLS,
reported the expected input values and worker identities, could not see the CLI's
source directory, and left no task directories after normal cleanup. The fixture
is materially different from a shared-filesystem demo. It remains one Docker host,
not physical-machine or ARM64 execution evidence.

General dependency/directory packaging, larger streaming uploads, arbitrary output
artifact retrieval, durable batch metadata, namespaces/cgroups,
and physical-machine benchmarks are still open. The bundled JSON-summary example
is a workflow demonstration with synthetic inputs, not a performance experiment.

## Benchmark infrastructure (November 3)

Map metadata now includes monotonic elapsed_seconds from client submission through
result receipt, excluding result persistence and the unsubmitted client backlog.
It includes network/authentication, placement wait, staging, execution, and result
delivery. It is not scheduler dispatch latency. A regression checks that the known
0.8-second task execution interval is included.

The benchmark harness compares direct sequential execution, matching local-parallel
subprocesses, and Forge worker counts using identical deterministic prime-counting
inputs. Trial division supplies CPU work; an independent sieve supplies the oracle
outside timing. Correct results must include task identity, the exact integer answer,
workload hash, valid telemetry, successful storage, and worker participation.

Full batch timing includes process startup, input handling/staging, execution, and
result writes, while excluding cluster setup and post-run correctness verification
for both baseline and Forge modes. Seeded order randomization, recorded warmups,
raw sample retention, and strict invalidation prevent failed/missing trials from
improving speedup claims. The report includes task p50/p95 using nearest rank,
batch median/spread, completed-task throughput, and comparisons against both
sequential and matching local-parallel baselines. Negative reductions remain visible.

Provenance records checkout commit/dirty state, binary/workload digests, compiler
and build type, control-host resources and visible cgroup limits, configuration,
worker identities/architectures/Python versions, and timing boundaries. A checkout
SHA is not attestation that an arbitrary supplied binary was built from it.
Topology declarations do not prove physical-machine separation. Credentials are
not exported. Follow [the methodology](../benchmarks/README.md) before using results.

Validation: all ten Debug CTest suites passed (114.90 seconds). Benchmark checks
cover known prime counts, raw observation preservation, throughput/sample accounting,
warmup exclusion, corrupted-answer rejection, failed/missing trial invalidation,
existing-result protection, and actual worker launch failure. The expanded
four-container TLS test also passed external-cluster benchmark smoke mode. CI
checks correctness only; tiny smoke timings are not performance evidence.

A Release experiment and its reviewed raw report can supply an initial one-host
overhead observation. Physical-machine scaling, representative workloads, recovery
measurements, utilization telemetry, and resume impact claims remain unverified.

Preparing the first optimized measurement exposed an existing Release-build
failure: GCC 13.3's optimized vector insertion diagnostic was promoted to an error
in frame encoding. Frame construction now allocates the final size once, fills
the length header, and copies the payload into that allocation. No warning flags
were disabled. Release subsequently passed all ten suites (114.51 seconds) and
the expanded four-container TLS test; Debug protocol checks also passed. CI now
builds/tests both Debug and Release, with the container-network check on Release.
This is build compatibility evidence, not evidence of an observed memory overflow.

Initial measured observation: source checkout f6fd4d2, Release, a single Docker
container with a two-CPU quota and 2 GiB RAM, and other development services running
on the PC. Four synthetic CPU inputs, one warmup per profile and three measured
repetitions yielded median batch times of 1.000 s direct sequential, 0.496 s direct
parallel (two processes), 4.044 s Forge with one worker, and 2.042 s Forge with two
workers. All 64 task executions including warmups passed the independent oracle.
Two-worker Forge took about 4.12 times as long as direct parallel execution here.
This is an unfavorable short-task overhead result, not a resume speedup claim.

The [reviewed raw report](../benchmarks/results/2026-11-03-local-001/README.md)
retains every trial, hashes, limits, summary, and the original git_dirty=true flag.
Windows Git was clean; Linux Git's CRLF view differed because it lacked the host's
global autocrlf setting. Ignoring CR-at-line-end and checking with that setting
showed no content differences. The flag was not rewritten. The summary and artifact
hashes were independently checked after export. The report discloses bind-mounted
source, native container result storage, shared-host load, and limited sample size.
Profile the 500-ms worker polling path and other phases before assigning the whole
difference to a component or claiming an improvement. Physical/real-workload and
recovery/utilization evidence remains pending.

## Immediate dispatch and observable runtime phases

The original worker slept on a 500-ms cadence before checking results and asking
for assignments. An idle heartbeat can now remain pending on the coordinator for
up to 500 ms. Scheduling wakes the handler through the registry condition variable,
which releases the mutex while waiting. The existing handler sends the assignment;
execution completion wakes the worker's connection thread through a separate
condition variable. Only that thread touches its socket/SSL object. Running jobs
retain periodic heartbeats. There is no new push stream or concurrent socket writer.

Exact ordered registration extensions select held heartbeats and profiled results.
Legacy workers remain usable on the updated coordinator; updated workers require
an updated coordinator. Extended worker results are normalized before client
delivery/history so run, map and logs keep their result schema. Authenticated
forge timings inspection returns per-host monotonic durations with unavailable
values represented as null. Reported worker timings are not independently proven;
no cross-host timestamps are subtracted and overlapping intervals are not additive.

The benchmark harness can interleave complete old/new runtimes and direct baselines
in the same shuffled rounds, recording both executable hashes and declared source
provenance. Optional phase inspection runs after batch timing. An invalid old/new
profile suppresses its comparison rather than filtering failures. Functional CI
checks pending-heartbeat dispatch, legacy results, invalid telemetry, repeated task
completion, running heartbeats and shutdown; it imposes no speed threshold.

Local validation: all eleven CTest suites passed in Debug (82.81 seconds) and
Release (82.11 seconds). The four-container TLS integration also passed staged
map, ordered artifacts, cleanup, external benchmark smoke, inspection and worker
loss checks. These establish regression correctness, not performance or physical
machine scaling.

Measured comparison: [raw evidence and reproduction](../benchmarks/results/2026-11-03-local-002/README.md).
The old bdde513 runtime and updated c0491fb runtime ran in the same two-CPU,
2 GiB Docker container with mutual TLS. Six profiles (direct sequential/parallel,
old/new one-worker/two-worker), one warmup plus five measured rounds, and four
identical inputs produced 144 validated executions, including 120 measured.
Two-worker median batch time decreased 75.81% (2.0432 s to 0.4943 s); direct
parallel took 0.5075 s. One-worker median decreased 76.20% (3.9948 s to 0.9507 s).
The current runtime approximately matches direct local parallel execution here.
This is evidence of removing runtime latency, not a claim that Forge beats direct
processes or scales across physical machines. Phase collection occurs after batch
timing; old/new phase attribution is unavailable because the old worker lacks it.

Potential XYZ resume bullet, with the scope retained: "Reduced median four-task
batch latency 76% (2.04 s to 0.49 s) in a two-worker local synthetic TLS benchmark
by replacing periodic worker polling with condition-variable dispatch and
completion wakeups." Broader distributed-performance claims require physical
tests and real workloads. All raw samples, background-load and CRLF provenance
caveats remain in the report; independent arithmetic and digest checks passed.

## Worker-loss recovery and attempt tracking (November 4)

run/map --retries 0..3 controls additional worker-loss attempts; zero is the
default. A stable logical job ID owns up to four numbered dispatch records.
Reservation creates an attempt with worker identity and state, not proof of exec.
Retryable placement requires attempt-v1, plus staging-v1 for staged tasks. Older
workers remain usable for ordinary jobs. The registry lock covers reservation,
attempt creation, loss/requeue, retry accounting and result acceptance.

On session loss the coordinator marks the attempt LOST, retires the worker and
releases its reservation before scheduling replacement capacity. Admission-time
staged bytes remain immutable in job memory and each attempt uses a fresh workspace.
Requeue resets its five-second capacity wait. Exhaustion yields LOST; no replacement
capacity yields UNSCHEDULABLE. Command failures and explicit timeouts stay terminal.
Shutdown cancels without retrying. Clients do not resubmit unknown outcomes or
failed result writes.

New wire messages carry job ID and attempt number. Only the current owning RUNNING
session can complete that attempt. Stale attempts, wrong jobs, duplicates and
unnegotiated result schemas reject the session instead of overwriting accepted
output. Reusing a retired worker ID does not reset the attempt counter. A rejected
session counts as worker loss and remains subject to the same bounded policy.

forge attempts JOB displays each worker/state, exit if known, reservation-to-end
duration and diagnostic. It uses the same authentication, active-job registry and
latest-64 finalized-job retention. Ordinary ps/result/log schemas remain; logs
show only final accepted output. Retry-enabled infrastructure failures retain
the admitted job ID in map ERROR records without inventing a command exit.
timings describes the current/last assigned attempt, not whole-job latency over
retries; client elapsed time includes all queue waits and attempts.

The key distinction is result fencing versus process termination. SIGKILL or a
partition can leave an old child running after a replacement starts. At-least-once
semantics allow repeated side effects; bounded retries guarantee neither execution
nor success. The test observes a surviving first child and two external writes,
then cleans up only its owned process group/workspace. Exactly-once execution,
durable coordinator recovery, automatic reconnect and namespaces remain
unimplemented. Cgroup enforcement was added subsequently, as described below;
it does not guarantee cleanup after worker loss. Only opt in when repetition is acceptable.

Coverage includes default loss, all four permitted attempts, exhaustion, unavailable
capacity, legacy gating, invalid retry options, reused IDs, stale-result rejection,
receive expiry, retained logs, structured map failure and shutdown. Real staged-map
recovery verifies the replacement receives admission-time bytes after source-file
mutation. Command exit 7, launch exit 127 and timeout 124 stay terminal with retries
enabled. The four-container mutual-TLS fixture stops the running job's worker and
verifies completion plus LOST/SUCCEEDED history on the survivor. These are one-host
correctness checks, not physical-failure or recovery-time measurements. Full policy
and protocol details are in [recovery.md](recovery.md).

The 75.81% benchmark remains evidence for source c0491fb against bdde513 in its
immutable report. It has not been remeasured for this extension; no new latency,
recovery-time, utilization or scaling number is claimed from test duration.

Local validation passed all twelve Release suites (100.65 seconds). The full
Debug suite passed (96.91 seconds), followed by final Debug execution/recovery
checks after tightening shutdown (28.72 seconds). The final Release binary also
passed the four-container mutual-TLS fixture with active-job recovery and attempt
inspection. CI runs the full Debug/Release matrix and the Release bridge test.

## Explicit cancellation and cleanup evidence (November 4)

forge cancel JOB uses the same TLS/token gate as other commands. Queued jobs
finish CANCELLED without dispatch; undelivered ASSIGNED attempts are withdrawn
under the registry mutex. Running jobs require cancel-v1 and enter CANCELLING.
The reservation stays held until a validated result or session retirement. The
same mutex orders dispatch, result acceptance, cancellation and retry. An accepted
result wins over a later cancel; an accepted cancel wins over later result receipt
even if execution already ended naturally. Cancellation does not roll back effects.

Stop instructions are heartbeat replies carrying exact job/attempt identity.
Only the connection thread touches SSL; execution observes an atomic flag and
performs ordinary process-group cleanup. The flag resets only for a new validated
assignment. An observed pre-existing cancellation prevents spawn; cancellation
racing with spawn cannot guarantee no command execution. Workspace destruction
precedes result delivery, but remains best effort.

Cleanup is pending, not-started, reported or unconfirmed. Lost contact, a five-second
acknowledgement budget, or coordinator shutdown preserves uncertainty. An earlier
LOST attempt makes overall cleanup unconfirmed even if the current attempt reports
cleanup. The earlier orphan may still run. Accepted cancellation always suppresses
retry, including cancellation between attempts. Older workers retain normal execution
support; unsupported running cancellation is rejected before changing the job.

run returns cancellation convention 130, distinct from an observed command exit;
attempts preserves the worker's exit when known. Normal commands exiting 130 remain
FAILED. Partial streams and truncation flags are retained when available. logs
retrieves cancelled results successfully; map stores CANCELLED and explicit cleanup
metadata while independent inputs continue. Cancelling a job does not cancel a
whole batch/backlog. Old clients must be upgraded for the new terminal result.

Tests cover queued/assigned/running transitions, idempotency, finished jobs, legacy
workers, reservation retention, retries, stale results, earlier lost attempts,
unresponsive workers, completion races and coordinator shutdown. A staged map job
spawns a child; tests verify both stop, the workspace disappears, partial streams
survive, and the next input succeeds on that same worker. A direct executor check
verifies already-cancelled work cannot create an external marker. Shared test helpers
keep recovery/cancellation protocol fixtures consistent. Benchmark admission now
recognizes CANCELLING as active work instead of treating the cluster as idle.

All fourteen local suites passed in Release (106.09 seconds) and Debug (106.61
seconds). The four-container mutual-TLS fixture also verifies staged cancellation,
directory cleanup and cancelled attempt history before exercising worker-loss
recovery. These are correctness checks on one host, not cancellation-latency,
physical-machine or isolation evidence. At this milestone cgroups were still open;
the subsequent implementation is described below. Namespaces remain open.
Details: [cancellation.md](cancellation.md).

## Visible capacity and cgroup-v2 enforcement

Workers now discover affinity and visible ancestor CPU quotas, flooring quotas to
whole CPUs. Memory is bounded by physical MemAvailable and ancestor
memory.max minus memory.current. Configured job parents are considered too.
Discovery fails closed on malformed controls or unavailable unified-v2 visibility.
Capacity is a registration-time snapshot; it does not track ongoing external load.

join --cgroup-root selects administrator-provisioned delegation. A confined
/bin/true startup probe validates actual migration and cleanup before registration.
run/map --enforce-limits requires cgroup2-v1 plus positive memory, with no fallback.
Each attempt has cpu.max, memory.max, zero swap for bounded jobs, and group OOM
handling. A private spawn helper joins before exec; helper startup and staging
allocations remain outside the job cap. Unconfigured workers retain accounting
only. Configured workers enforce CPU and nonzero memory requests for all jobs.

Normal completion, timeout and cancellation kill the cgroup, including setsid
descendants. Cleanup waits for populated=0 and removes the owned directory.
Setup failure returns 125 without launching unrestricted work; an observed OOM
counter adds a diagnostic, rather than inferring OOM from exit 137 alone.
Cleanup failure retires the worker and preserves cancellation uncertainty.
Worker SIGKILL can still leave orphaned groups. Privileged jobs can bypass these
controls; namespaces and credential filtering are not implemented.

Sixteen local suites passed in both Debug and Release, followed by actual-affinity
checks. The private TLS bridge passed. Separate real-kernel fixtures in both builds
verify CPU throttling, memory OOM, process-group escapes, timeout/cancel, staged
map, missing/readonly delegation, migration refusal, properly delegated unprivileged
execution, setup failure and cleanup-failure retirement. Parser fixtures alone
are not kernel evidence. These checks establish correctness in disposable
containers, not physical-machine scaling, new performance results or sandbox security.

Interview question: why both reservations and cgroups? Reservations avoid admitting
more requested capacity than advertised; cgroups bound actual consumption for
configured workers. Neither guarantees capacity against other host activity.
Why a startup probe? Writable controls alone do not establish permission to migrate
the workload from the worker's leaf to the delegated job child. Why retire after
cleanup failure? Reusing that worker would hide uncertain remaining resources.
Full setup and limitations: [resource-enforcement.md](resource-enforcement.md).
