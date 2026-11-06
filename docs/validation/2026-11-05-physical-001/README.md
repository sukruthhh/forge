# Initial two-machine execution evidence

Observed November 5, 2026, at 17:02 UTC. This is a correctness observation,
not a workload benchmark or a complete physical-cluster acceptance test.

Two physical Windows machines ran native x86_64 Ubuntu WSL2 workers:

| Worker ID | Reported Linux hostname | Advertised whole CPUs | Advertised memory headroom |
|---|---|---:|---:|
| desktop-native | SukruthsPC | 28 | 15,524,569,088 bytes |
| laptop-native | SukruthPC | 8 | 7,186,182,144 bytes |

The PC hosted the coordinator, desktop worker and submission runner. The laptop
initiated a key-authenticated SSH tunnel to the PC; Forge mutual TLS remained
enabled inside it. SSH allowed forwarding only to the loopback coordinator and
disabled command sessions. Neither worker enabled cgroup enforcement in this run.
The machines shared an apartment network; no router forwarding was configured.

## Observed results

The runner submitted two jobs concurrently. Each staged a Python probe and a
fresh JSON input created in a temporary directory on the PC. These files were
transferred by Forge rather than placed in shared storage. The probe read its
input, slept for three seconds, and returned hostname, architecture, working
directory, input hash and a unique value from the input.

- job-2 succeeded on desktop-native, exit 0, with one accepted attempt.
- job-3 succeeded on laptop-native, exit 0, with one accepted attempt.
- Both returned input SHA-256 values and unique values matched their submissions.
- Both reported distinct /tmp/forge-job-* staging directories.
- Both workers were READY before and after execution.
- Retained ps output also records the preceding laptop-submitted /bin/hostname
  job succeeding on the desktop. Its original CLI output was supplied by the user;
  the runner did not capture that submission itself.

## Provenance and limitations

[results.json](results.json) is the unmodified saved runner output.
[runner-original.py](runner-original.py) preserves the original runner, including
its test-specific paths and declared PC source revision c5ba401. It regenerates
random input values and requires a configured, idle two-worker cluster; it is
not a general installation script. Original temporary inputs were removed by
the runner, but their content can be reconstructed from index and expected_nonce.

The PC binary SHA-256 is recorded. It was built and tested locally from c5ba401;
the source label alone is not a cryptographic build attestation. The laptop build
and sixteen passing suites were supplied through the user's terminal output;
its binary hash, full checkout revision and hardware model were not collected
in this observation. Worker capability and hostname reports are trusted claims,
not independently attested identities. Physical topology was established through
the device setup in this development session.

Durations include an intentional sleep and have only one sample per machine.
They must not be turned into throughput, speedup or latency claims. Python and
Ubuntu versions differed between the devices. No ARM64, GPU, resource enforcement,
failure recovery, authentication-negative test, usability or adoption result is
established here. Separate benchmark trials and fault injection remain necessary.

Afterward, the coordinator, desktop worker and temporary SSH listener were
stopped. Windows forwarding/firewall cleanup commands were provided to the user;
their removal was not confirmed at that time. Test credentials remain outside Git.
