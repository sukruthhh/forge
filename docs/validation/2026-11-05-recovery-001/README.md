# Physical-cluster worker-process crash recovery

Observed November 5, 2026, Toronto time (November 6 UTC). Five deliberate crashes
of the PC worker process all recovered on the physical laptop. This is not
physical power-loss, heartbeat-silence or network-partition evidence.

## Procedure and results

The idle PC/laptop WSL2 cluster used runtime c5ba401, the same PC binary as the
[physical synthetic benchmark](../../../benchmarks/results/2026-11-05-physical-001/README.md).
The coordinator and PC worker used loopback mutual TLS. The laptop remained
connected through mutual TLS inside restricted SSH. Neither worker enabled the
optional cgroup backend. The fixture did not disconnect the laptop tunnel.

For each trial, the runner submitted two retry-safe staged probes with --retries 1
and a 15-second per-attempt timeout. Each probe read a unique JSON input, slept
eight seconds and returned its input hash, unique value and hostname. After both
were RUNNING on different workers, the fixture cancelled the laptop's probe and
waited for cancellation acknowledgement, freeing replacement capacity. It then
verified the PC worker PID/command and its spawned probe before sending SIGKILL
to that worker only.

| Observation across five injected crashes | Median | Range |
|---|---:|---:|
| Signal request to first inspection observing LOST | 7.13 ms | 6.65–20.24 ms |
| Signal request to completed submission result | 8.1145 s | 8.0630–8.2535 s |

All five victim jobs completed successfully on laptop-native, with exactly two
attempts under the same job ID: desktop-native LOST, then laptop-native SUCCEEDED
with exit 0. Returned hashes and unique input values matched the original staged
input in the live fixture. The five placement probes cancelled on the laptop
returned cancellation convention 130. The PC worker was restarted after each trial.

Both intervals use the observer PC's monotonic clock. LOST was observed through
repeated authenticated CLI inspection; polling and inspection delay are included.
It is an observation upper bound, not pure failure-detection or scheduler latency.
Crashing a loopback-connected worker closes its socket promptly; a silent partition
would exercise different detection paths and cannot inherit this number.

Signal-to-result includes replacement admission, staging, execution (including the
intentional eight-second sleep) and response delivery over SSH/TLS. Do not report
8.11 seconds as generic application recovery time or subtract eight seconds to
claim precisely measured recovery overhead. There is no uninterrupted paired
baseline or broad tail-latency evidence; five observations are a small sample.

## Cleanup and preflight failures

The old child remained visible in /proc after every worker crash. Process visibility
alone does not establish its running state. The fixture checked its original
start identity before killing the test-owned process group, and removed only the
captured /tmp/forge-job-* workspace after validating the staged probe bytes.
This administrative cleanup was performed by the fixture, not crash recovery in
Forge. Result fencing does not guarantee stopped old work or exactly-once effects.

Two earlier fixture runs aborted before injecting a crash:

- preflight-main-thread.json: looked for the spawned child only under the main
  thread. Execution uses an async thread; the corrected lookup covers all threads.
  Both test jobs were cancelled.
- preflight-history.json: reused filenames matched retained jobs from that first
  run. The two new probes completed without a crash; unique per-run filenames
  fixed the fixture's selection. No runtime recovery failure is hidden here.

Both failed setup reports are retained. The five crash trials were then performed
in one invocation; no failed crash trial was discarded or repeated.

## Artifacts and reproduction

results.json preserves the five raw trial records, signal intervals, CLI streams,
cancellation results and attempt snapshots. runner-original.py is the exact runner
used, with test-specific paths and the PC worker's PID file. It can terminate and
restart that verified worker; it is a test fixture, not a general CLI command.
Temporary original input files were removed, and complete laptop binary provenance
was not collected. Input bytes can be reconstructed from the returned nonce and
the task index in the recorded assignment command; the live fixture also checked
the original input before removing it.

To reproduce, configure an idle two-worker PC/laptop cluster as above, update the
explicit binary/credential/PID paths for that test cluster, and run the fixture
from an existing writable evidence directory on the PC. It requires ownership
of the designated desktop worker and records output beside itself. Never point
its PID file at an unrelated process. Its cleanup is limited to its own staged
jobs and identity-checked process group; it does not clean arbitrary old jobs.

Independent inspection of saved results:

```sh
python3 docs/validation/2026-11-05-recovery-001/review.py
```

The defensible claim is recovery in five controlled PC-worker-process crash trials
across two physical hosts. It does not establish a reliability percentage,
coordinator durability, reconnection, machine-failure recovery or secure isolation.
