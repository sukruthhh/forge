# Resource discovery and cgroups v2 enforcement

```sh
forge join --id limited-worker --cgroup-root /path/to/delegated/jobs
forge cluster --details
forge run --enforce-limits --cpu 1 --memory 67108864 -- python3 task.py
forge map --enforce-limits --memory 67108864 --file task.py --output results \
  inputs/a.json inputs/b.json -- python3 task.py '{}'
```

--enforce-limits requires positive CPU and memory requests and a worker advertising
cgroup2-v1 (version 1 of Forge's cgroup-v2 execution capability). The coordinator
never falls back to a worker without it. Map checks compatible idle capability
before creating output. Without the flag, CPU/RAM requests remain placement hints
on unconfigured workers. Configured workers apply CPU limits and any nonzero memory
request to every job they receive; memory 0 adds no per-job memory cap and inherits
the parent hierarchy. It is rejected with --enforce-limits.

## Capacity discovery

Workers now report sched_getaffinity CPU availability, constrained by cpu.max at
their current cgroup and every visible ancestor. Quotas are floored to whole CPUs
because the scheduler's resource model is integral. A worker below one whole CPU
refuses to register; fractional requests are not implemented. Affinity incorporates
cpuset restrictions. CPU hotplug/affinity changes after registration are not tracked.

Memory starts at physical MemAvailable (bounded by physical total) and is further
bounded by each finite ancestor memory.max minus memory.current, saturating at
zero. A configured worker also considers the delegated job hierarchy. This is a
conservative registration-time headroom snapshot, not a reservation against other
host processes or live utilization telemetry. External load and reclaimable cache
can change availability. No-headroom workers fail registration rather than report
impossible capacity. Namespace-hidden ancestor limits cannot be discovered here.

Discovery resolves the worker's unified membership through /proc/self/cgroup and
/proc/self/mountinfo, including escaped mount paths. Missing unified-v2 visibility,
invalid controls and read failures fail closed. cgroup-v1 discovery is not supported.
cluster keeps its old schema; --details adds staging, attempt and enforcement
capabilities and reports BUSY workers. Existing six- through eleven-field worker
registrations remain valid; their claims are trusted rather than attested.

## Provisioning a delegated hierarchy

Use an administrator-provisioned cgroup v2 domain with cpu/memory enabled in its
cgroup.subtree_control. The path must be inside the worker's resolved visible v2
mount; alternative bind aliases outside that mount are not supported. The job
parent must have no resident processes. The worker
must have permission to create/remove children, write their controls, migrate its
helper from the worker leaf into a job child, and use cgroup.kill. This requires
Linux support for cgroup.kill (5.14+) and the memory/swap controller interfaces.
Forge does not change ancestor controllers or move the worker automatically.

A suitable layout is:

```text
delegated unit (cpu and memory enabled, no resident processes)
  manager/          worker process
  jobs/             empty parent, cpu and memory enabled
    forge-PID-N/    one job attempt, created/removed by Forge
```

systemd Delegate=yes can provision management of a dedicated unit's subtree;
available controllers still depend on the administrator and parent hierarchy.
An illustrative launcher, executed as the sole process of that delegated unit on
a unified hierarchy mounted at /sys/fs/cgroup, is:

```sh
#!/bin/sh
set -eu
membership=$(sed -n 's/^0:://p' /proc/self/cgroup)
test -n "$membership" && test "$membership" != /
parent="/sys/fs/cgroup$membership"
mkdir "$parent/manager" "$parent/jobs"
printf '%s\n' "$$" > "$parent/manager/cgroup.procs"
printf '+cpu +memory\n' > "$parent/cgroup.subtree_control"
printf '+cpu +memory\n' > "$parent/jobs/cgroup.subtree_control"
exec /absolute/path/to/forge join --id worker-a --cgroup-root "$parent/jobs"
```

For example, launch that script using systemd-run --user --unit=forge-worker
--property=Delegate=yes /absolute/path/to/launcher.sh. Configure the normal TLS/token
environment for the service as needed. This administration example has not been
validated against this laptop's native Ubuntu service manager; the tested fixture
uses an isolated container hierarchy. Do not run the launcher in a shared unit or
against the host hierarchy root. Missing delegation is an actionable setup error,
not a reason to run all workloads privileged or silently disable enforcement.

Before registration Forge creates a temporary child, configures its controls,
launches a confined /bin/true with a 32-MiB probe cap and cleans it up. Writable files alone are insufficient:
the probe also checks migration permission. Invalid/readonly hierarchies, missing
controllers and a failed probe prevent cgroup2-v1 registration.

## Execution and failure behavior

Each attempt receives a fresh cgroup. CPU bandwidth uses cpu.max with a 100000-us
period and quota equal to requested whole CPUs times that period. This is an
aggregate fair-class bandwidth cap, not exclusive cores or a wall-time guarantee.
memory.max sets the requested accounted-memory cap (kernel page granularity);
bounded jobs also set memory.swap.max=0 and memory.oom.group=1.

The existing posix_spawn process-group/pipe setup launches a private internal entry
of the same Forge executable. Its directory descriptor is explicitly inherited;
other descriptors stay close-on-exec. The single-threaded helper selects SCHED_OTHER,
joins its job through cgroup.procs, protects the descriptor, then execs the command.
The actual workload never starts outside its prepared cgroup. Helper startup and
worker staging/network allocations are outside the job cap. The __cgroup-exec helper is
not an installation API or a privileged service.

Setup failure returns 125 and never launches the workload unrestricted. Launch
failure remains 127. An observed memory.events OOM-kill counter adds a bounded
diagnostic; exit status remains what waitpid observed (the fixture sees 137).
OOM failures are terminal command results, not worker-loss retries. Exit 137 by
itself is not treated as proof of OOM.

Normal completion, timeout, cancellation and unwinding use cgroup.kill as well as
ordinary process-group cleanup. The worker waits for populated=0 before removing
its owned directory; this includes descendants that called setsid. If cleanup
cannot finish or the directory cannot be removed, the worker retires without
acknowledging successful cleanup. Pending cancellation remains unconfirmed and is
not retried. Other opted-in jobs retain the documented worker-loss policy.

Groups have exclusive PID/counter names; collisions never overwrite existing work.
No automatic crash-orphan scavenger exists. SIGKILL or machine failure can leave
groups/processes behind, still subject to surviving kernel limits. A service
manager or administrator must handle teardown; see worker logs for setup/cleanup
errors. Forge never recursively deletes arbitrary hierarchy directories.

## Boundaries and evidence

These controls do not provide namespaces, filesystem/network isolation, credential
filtering, a PID limit or protection against privileged hostile commands. A job
with permission to alter cgroups, scheduling policy or privileges can bypass the
intended policy. Use trusted workloads and appropriate delegation; do not call this
a secure sandbox. CPU/RAM limits are caps, not guaranteed capacity or performance.

CTest checks ancestor-limit parsing, affinity/headroom bounds, malformed controls,
ordinary-filesystem rejection, unenforced-worker exclusion and map preflight.
Parser fixtures are explicitly not kernel enforcement evidence. Separately:

```sh
python3 scripts/test-cgroup-enforcement.py --binary build/release/forge
```

That required CI fixture creates a disposable privileged container with a private
cgroup namespace, two-CPU quota and 512-MiB cap. It changes only that container's
hierarchy, mounts repository source read-only, and cleans up the named container.
It verifies real CPU throttling, memory OOM, fractional-quota refusal,
migration-permission refusal, correctly delegated unprivileged execution, staged
map, setsid descendants, timeout/cancel, controller removal during setup, readonly
delegation and cleanup-failure retirement. Only that container's subtree is altered;
unrelated host and other-container cgroups are not modified. This is
correctness evidence, not physical-machine validation or a benchmark.

References: [Linux cgroup v2 interfaces](https://docs.kernel.org/admin-guide/cgroup-v2.html),
[systemd delegation](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html),
[systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html).
