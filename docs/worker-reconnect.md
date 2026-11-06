# Worker reconnection

```sh
forge join --id laptop --host COORDINATOR_IPV4 --reconnect
```

Configure the normal TLS environment for non-loopback endpoints. --reconnect is
opt-in; without it, a transport failure still ends the worker with a nonzero exit.
The flag keeps the same worker process trying to establish a fresh session after
connection refusal, disconnect or I/O deadline expiry. It does not restart a dead
worker process, repair an SSH tunnel, persist coordinator state or enable job retries.

## Backoff and admission

Retries sleep for 250, 500, 1000, 2000, 4000, then at most 5000 milliseconds, plus
0–100 milliseconds of jitter. Each retry logs its transport reason and planned
wait. Successful heartbeat responses spanning ten seconds reset the next backoff
to 250 milliseconds. Merely opening a connection or receiving registration OK
does not reset a flapping connection's backoff.

The cap applies to the sleep, not total recovery time. Existing connect, TLS,
authentication and frame deadlines still apply, as does old-job cleanup. Backoff
checks SIGINT/SIGTERM every 25 milliseconds; in-flight operations keep their
existing deadlines. Retry continues until stopped or a fatal error occurs.

Every session rediscovers visible CPU/memory capacity, reloads TLS files and, when
configured, probes cgroup delegation again. Credential files can be rotated at
their configured paths. The process does not acquire changed environment values
from a different terminal; changing token values or credential paths requires
restarting it with the new environment.

The coordinator still refuses to replace a connected worker ID. A first observed
worker-ID-busy rejection is fatal. After this process loses transport during or
after a registration attempt, busy rejections are retried while the old session
retires. This covers a lost registration acknowledgement without evicting a live
session. There is no takeover operation, new identity attestation or certificate
binding of worker IDs. Persistent ID contention requires operator attention.

There is no wire extension. The client recognizes the existing established-session
ERROR text I/O deadline exceeded as a transport timeout. Other application/protocol
errors are fatal. Worker-ID-busy classification likewise uses the existing exact
admission error. These compatibility cases do not turn arbitrary server errors
into retries.

## Old execution and uncertain results

On disconnect the worker shuts down its old connection, sets the execution cancel
flag and consumes the execution future before attempting new admission. This
waits for the existing executor's cleanup path. Old job IDs, attempt numbers,
completion flags and output are discarded; no result or assignment is replayed.
Losing the acknowledgement of a result does not cause the worker to resend it.

Ordinary workers retain process-group cleanup and best-effort workspace removal;
escaped descendants and removal failures are not magically resolved by reconnect.
Configured cgroup workers retain their stronger descendant cleanup. A reported
cgroup cleanup failure, including one raised while unwinding a transport failure,
is fatal: the worker exits instead of advertising uncertain capacity again.
These are trusted-workload controls, not a secure sandbox.

The coordinator alone owns job retry policy. --retries 1..3 remains explicit
caller opt-in with at-least-once semantics. It can schedule a replacement while
the old worker has not yet detected loss; repeated effects remain possible.
Reconnection does not change a cancelled job, restore lost output, promise exactly
once execution or resubmit an uncertain CLI request. See [recovery](recovery.md).

After a coordinator restart, registration can succeed and new jobs can run, but
old in-memory history and accepted submissions are still lost. Fresh session state
prevents an old result from being mistaken for a new job with a reused ID.

## Fatal failures and setup diagnostics

Malformed framing/assignments, token rejection, TLS certificate/protocol rejection,
invalid endpoint/configuration, resource-discovery/delegation failures and failed
resource cleanup do not reconnect. TLS unexpected EOF, clean close and syscall
transport failure are retryable; other TLS protocol errors remain fatal. See
[OpenSSL error semantics](https://docs.openssl.org/3.0/man3/SSL_get_error/).

Local TLS errors now identify FORGE_TLS_CA, FORGE_TLS_CERT or FORGE_TLS_KEY rather
than one generic configuration message. Check the named file, PEM encoding and
certificate/key match. TLS private keys must be unencrypted, as before; Forge
does not prompt for their passphrases. SSH key passphrases are a separate mechanism.
Diagnostics do not print key contents or token values. There is no insecure
fallback or change to certificate verification.

## Validation scope

The reconnect suite covers initial offline startup, repeated failures through
capped backoff, stop during backoff, default exit behavior, client/server timeout,
busy IDs after admitted or uncertain registration, rejection of a new duplicate,
fatal protocol/token/certificate errors, TLS transport loss and credential-file
rotation. It verifies refreshed affinity capacity, active staged-process/workspace
cleanup before admission, lost result acknowledgements, reused job IDs and a real
coordinator restart with the same surviving worker process.

The private kernel fixture checks that --reconnect cannot override failed cgroup
cleanup, including when transport loss and cleanup failure coincide. Separate
Debug/Release regression suites and the mutual-TLS bridge protect existing behavior.
These automated checks are not measurements of real-network reconnect latency.
The earlier PC/laptop experiments used older workers without this flag; do not
retroactively describe their manual restart as automatic recovery.
