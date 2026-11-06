# Development-assisted onboarding observations

These observations come from helping the project owner set up the PC/laptop
test fleet on November 5–6, 2026. They are qualitative development feedback, not
independent customer research, adoption evidence or a timed usability study.
No setup-time reduction, satisfaction score or general success rate was measured.

| Observed problem | Evidence and response | Remaining product work |
|---|---|---|
| Missing Linux prerequisite and stale checkout | Laptop had no named Ubuntu distribution and initially showed an older Forge revision; installation/build instructions were needed | A prerequisite checklist and explicit revision/build verification |
| Missing TLS trust bundle | Client returned a generic TLS configuration error; file inspection confirmed trust.pem did not exist | Implemented variable-specific TLS load errors; broader preflight remains open |
| Windows executable lookup | netsh was absent from command lookup despite existing in System32; absolute-path commands worked | Reproducible WSL instructions with exact terminal/device labels |
| Temporary SSH host key disappeared | Restarting the environment lost the ephemeral key, causing a legitimate host-identification warning | Persistent root-owned test host key and explicit fingerprint verification; never suppress host checks |
| Foreground tunnel state was unclear | Silent ssh -N behavior and passphrase prompts required explanation; cluster checks exposed missing listeners | A clear readiness check and first-run troubleshooting flow |
| Environment was terminal-specific | TLS variables had to be configured in the second Ubuntu terminal | Document scope or provide an explicit supported configuration mechanism |
| Idle worker exited on an I/O deadline | Laptop terminal showed registration followed by I/O deadline exceeded; registry showed DEAD and a benchmark rejected the fleet | Implemented opt-in --reconnect with backoff and cleanup/admission tests; physical deployment verification remains separate |

The underlying cause of the laptop I/O stall was not isolated. The SSH connection
remained present; that does not prove the worker's application connection is live.
Manual join restored that older worker. The subsequent opt-in
[reconnect implementation](worker-reconnect.md) does not change what happened in
the recorded experiment or establish a user-outcome improvement by itself.
Likewise, installing a tunnel and restricting its listener is not an evaluation
of hostile-workload isolation or the apartment router's network segmentation.

## Next usability work

Variable-specific TLS diagnostics and opt-in reconnect now address two of these
problems. A repeatable first-run guide and broader preflight remain open. Reconnect
tests enforce session/result separation, refreshed capacity and refusal after
failed cgroup cleanup. No timed or independent retest has yet established how much
these changes improve the onboarding experience.

Then ask an independent participant to follow the guide on a supported Linux
environment and complete a defined task: install/build, join, run a staged input,
retrieve its result and stop the worker. Record their starting environment,
credential/network prerequisites, elapsed time, errors and every assistance step.
Keep the task and measurement boundary consistent when retesting improvements.
Record unsuccessful attempts too. A small sample supports a case study, not a
claim about developers generally. Participation has not yet been arranged.

The performance experiments also expose a separate product tradeoff: one job per
worker is simple, but does not automatically exploit all cores on a large PC.
Both physical workloads lost to two direct PC processes. This motivates evaluating
concurrency and workload fit; it does not prove that one scheduling change would
reverse the result. See the published benchmark reports for exact comparisons.
