# Same-process laptop reconnection after coordinator restart

Observed November 6, 2026, at 06:00 UTC (01:00 Toronto). One controlled idle
coordinator restart verified automatic readmission of the same physical laptop
worker process. This is a correctness observation, not a reconnection-latency
benchmark, active-job recovery test or durable coordinator restart.

## Procedure and observed results

The PC hosted the coordinator and control CLI. The physical laptop ran native
Ubuntu WSL2 and connected using mutual TLS inside its restricted SSH tunnel.
The laptop had pulled/rebuilt 9e34fac and started with --reconnect. Its source
checkout was clean, and a job confirmed that its live worker executable matched
the installed binary. Neither worker used the optional cgroup backend in this test.

The runner checked that no job was active, stopped the PC worker so both probes
would execute on the laptop, and ran an identity probe. It then stopped the
existing coordinator, left it down for three seconds after exit, and started the
new coordinator. The SSH tunnel stayed open; the laptop worker was not restarted.
After laptop-native returned to READY, a second probe ran there. The PC worker
was then started with --reconnect and both workers were READY at the end.

| Identity / result | Before | After |
|---|---|---|
| Reported hostname | SukruthPC | SukruthPC |
| Worker PID | 1607 | 1607 |
| Linux process start ticks | 8197977 | 8197977 |
| Source revision | 9e34fac | 9e34fac |
| Reconnect enabled | true | true |
| Job ID | job-111 | job-1 |
| Exit | 0 | 0 |

Both jobs returned their separately generated expected nonce. Matching PID,
start ticks, host and executable hash support same-process continuity; the new
nonce establishes that the later result was fresh. Job numbering reset because
the new coordinator has no durable history. The test does not restore old jobs.

The observer recorded 0.9813 seconds from spawning the replacement coordinator
until its first inspection observing the laptop READY. That includes coordinator
startup, backoff alignment, TLS, registration and polling. It is one observation,
not a median, upper bound, pure reconnect latency, SLA or resume speed claim.

Before this controlled test, the owner supplied a console log showing connection
refusal retries followed by registration after manually restoring the SSH tunnel.
tunnel-restoration-excerpt.txt preserves the last eight lines of that supplied
log. It is an excerpt, not the full original console log or an independent PID
measurement. The controlled restart above supplies the separate identity check.

## Provenance and limits

The control CLI, restarted coordinator and updated workers used source
9e34fac12a60cbb5d2ad24d586a65aca8e96d57f. The older coordinator serving the first
probe came from the existing development session; its executable hash was not
captured before shutdown. No protocol extension was needed for the new worker.

PC binary SHA-256:
149ccec7599e00a84403ea10044540a97f1b0693274edbc0f9c2569616492974.
Laptop live/installed binary SHA-256:
2a7adb04c63d82bed167ecee525348b8caee1313c829ce4062b1199204e76071.
Hashes identify the executables, not cryptographic source-build attestation.
The laptop reported x86_64, Linux 6.6.114.1-microsoft-standard-WSL2 and Python
3.14.4. Binary hash and process start identity were collected on both sides of
this restart; earlier performance reports did not include that laptop hash.

results.json is the unmodified raw runner report. runner-original.py preserves
the test-specific paths, expected source revision and verified test-process PID
files. It stops/restarts the test coordinator and PC worker, requires an idle
cluster, and writes results beside itself. Reproduction requires those paths
and the reviewed source revision to match a dedicated test fleet; it is not a
general installation or failover script.

The coordinator and new PC worker remain running afterward. Windows forwarding
and laptop-only SSH firewall rules were unchanged. Keys remain outside Git.
This does not test network partition duration, machine power loss, crash cleanup,
changed endpoint discovery, long-term availability or independent onboarding.
Worker reconnect cannot repair an SSH tunnel or persist accepted submissions.
The implementation and automated cases are described in [worker reconnection](../../worker-reconnect.md).

Independent saved-report checks:

```sh
python3 docs/validation/2026-11-06-reconnect-001/review.py
```
