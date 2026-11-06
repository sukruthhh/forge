# Concurrent jobs on one worker

The default remains one job per worker. Opt in to bounded concurrency:

```sh
forge join --id desktop --slots 2 --reconnect
forge cluster --slots
forge map --jobs 2 --file task.py --output results input-a.json input-b.json -- python3 task.py '{}'
```

`--slots` accepts 1..16 and limits simultaneous assigned/running jobs, not threads
inside a command. A worker still registers one identity and one CPU/RAM capacity.
The coordinator reserves each job's requested resources under the registry lock;
the sum cannot exceed that worker's advertised CPU or RAM. Each job requests at
least one CPU, so adding slots cannot bypass CPU admission. Slots do not partition
capacity: a two-CPU command can use a two-CPU worker's entire budget even when it
has two slots. A zero memory request reserves no RAM and provides no memory bound.

`cluster --slots` reports the configured slots, active reservations and reserved
CPU/RAM. Partial capacity remains eligible for slot-aware map admission; the map
client caps its own concurrency at the compatible free slots and resource budget
observed at startup. This is a snapshot, not a guarantee against competing clients.
The existing five-second placement deadline still applies.

One control connection carries heartbeats, assignments, attempt results and stop
requests. Each execution has independent cancellation state, future, workspace and
optional cgroup. Cancellation requests rotate among pending jobs, retain repeated
delivery, and take priority over dispatch. Reservations remain until the result
acknowledges cleanup, or the session is lost. Every result must match a job and
attempt owned by that session. A lost session processes all of its jobs separately
under their existing retry/cancellation rules.

With spare slots and running work, the coordinator holds pending heartbeats for
up to 25 ms and wakes on new assignments/cancellations. The worker immediately
renews these requests. This avoids the older 500-ms active-heartbeat dispatch gap
while bounding how long the one control thread can wait before sending a finished
result. Idle workers retain 500-ms pending requests; full workers retain their
500-ms heartbeat interval and completion notifications. This is a bounded
long-poll tradeoff, not fully asynchronous bidirectional streaming.

After transport loss, the worker cancels and consumes every execution before
reconnecting. Failure to clean a cgroup retires the whole worker, including when
another job or a transport error fails at the same time. Old results are not
replayed. Reconnection and job retry remain independent opt-ins.

Configured cgroup workers create separate CPU/memory limits per job. Without a
cgroup delegation, resource requests are admission accounting only. Concurrent
commands share the worker user's privileges, environment, filesystem and network;
they are trusted workloads, not securely isolated tenants. Host activity can
reduce usable capacity after registration, and registration snapshots are not live
utilization telemetry.

## Compatibility and validation

Multi-slot registration appends `slots-v1`, then a decimal slot count, after
`cancel-v1` and the optional `cgroup2-v1` extension. The default one-slot worker
retains its previous registration. Older coordinators reject multi-slot workers;
upgrade the coordinator before enabling slots. Existing snapshot schemas remain
unchanged. New slot-aware clients use message IDs 41/42; map can fall back to the
older capability query when an older coordinator returns an error.

The concurrency integration suite checks CPU/RAM admission, resource release,
simultaneous worker-loss retries, two running jobs on one identity, cancellation
without stopping a sibling, staged map results and multi-child shutdown cleanup.
The real-kernel fixture verifies independent concurrent cgroups and an OOM kill
that leaves the sibling alive. The benchmark harness can compare slot settings
with matching direct-parallel baselines; correctness tests do not imply a speedup.

An example controlled, same-binary comparison is:

```sh
python3 benchmarks/run.py --binary build/release/forge \
  --compare-binary build/release/forge --comparison-source same-binary-one-slot \
  --workers 1 --worker-slots 2 --comparison-slots 1 --tasks 8 \
  --workload oscillator --steps 2000000 --repetitions 5 --warmups 1 \
  --environment-kind wsl --label slot-comparison --output build/benchmarks/slots
```

The experiment uses managed local worker processes, not physical-machine scaling.
The harness records both slot settings, binary hashes, all warmups and failed
trials. Performance comparisons use complete batch durations, not test run times.

During refinement, one local Debug benchmark smoke failed before recording a
batch duration. Its temporary fixture did not retain a useful failure cause;
it remains unexplained. Trial diagnostics are now included in test failures.
Three repeated benchmark-suite runs, 32 retained diagnostic smoke trials and a
subsequent full Debug run passed. These reruns do not establish that the
intermittent issue is resolved; the retained measurement experiments are separate.
