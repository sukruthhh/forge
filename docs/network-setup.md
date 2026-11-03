# Authenticated network endpoints

Forge supports numeric IPv4 endpoints with mutual TLS 1.3 through OpenSSL 3.
The coordinator accepts `--bind ADDRESS`; join, cluster, run, ps, and logs accept
`--host ADDRESS`. Both default to 127.0.0.1. DNS names and IPv6 are not supported
in this increment. Avoiding DNS keeps connection setup free of blocking resolver
calls; it trades hostname convenience for explicit IP-based verification.

Without TLS configuration, only addresses in 127.0.0.0/8 are accepted. Any other
bind or destination, including 0.0.0.0, requires all three environment variables:

| Variable | Meaning |
|---|---|
| FORGE_TLS_CA | PEM trust anchor bundle for cluster membership |
| FORGE_TLS_CERT | This peer's PEM certificate chain |
| FORGE_TLS_KEY | Matching PEM private key |

Keys must be readable without an interactive passphrase. Encrypted private keys
fail configuration rather than prompting; protect key files with filesystem permissions.

The coordinator certificate needs serverAuth usage and an IP subject alternative
name matching the client's --host address. Workers and submitting/inspection
clients need clientAuth certificates signed by a CA the coordinator trusts.
Verification is mandatory; there is no insecure bypass or plaintext fallback.
Partially configured TLS, missing files, and mismatched keys fail before binding
or connecting. FORGE_TOKEN remains an optional additional gate inside TLS.

## Development credentials

On Linux, with Python 3 and openssl installed:

```sh
python3 scripts/create-dev-certificates.py \
  --output "$HOME/forge-dev-credentials" --server-ip 192.168.1.50
```

Replace the example with the coordinator's reachable private IPv4 address. The
helper creates a new owner-only directory, a private development CA, a server
certificate/key, and a client certificate/key. Credentials expire in seven days.
It refuses to overwrite an existing directory. Keep all keys outside Git.

For a two-machine trusted development test, keep server.crt/server.key and ca.crt
on the coordinator. Copy only ca.crt and client.crt/client.key to the worker/client
using a trusted transfer such as SSH. Keep ca.key private on the issuing machine;
do not distribute it. This helper shares one client identity for a development
fixture, not a complete onboarding or certificate-management system. Separate
peer certificates, rotation, and revocation need an explicit operational policy.

On the coordinator:

```sh
export FORGE_TLS_CA="$HOME/forge-dev-credentials/ca.crt"
export FORGE_TLS_CERT="$HOME/forge-dev-credentials/server.crt"
export FORGE_TLS_KEY="$HOME/forge-dev-credentials/server.key"
./build/forge coordinator --bind 192.168.1.50 --port 9000
```

On a Linux worker, using its protected credential paths:

```sh
export FORGE_TLS_CA="$HOME/forge-peer/ca.crt"
export FORGE_TLS_CERT="$HOME/forge-peer/client.crt"
export FORGE_TLS_KEY="$HOME/forge-peer/client.key"
./build/forge join --host 192.168.1.50 --port 9000 --id laptop
```

In another Linux terminal with client credentials configured:

```sh
./build/forge cluster --host 192.168.1.50
./build/forge run --host 192.168.1.50 -- /usr/bin/printf 'hello from a worker\n'
./build/forge ps --host 192.168.1.50
./build/forge logs job-1 --host 192.168.1.50
```

Commands and inputs must already exist on workers. All executables still target
Linux; these examples do not establish a native Windows or macOS CLI/backend.
Permit the coordinator's TCP port only from the intended private peers. Certificate
admission is not a reason to expose the development runtime to the public Internet.

## Transport ownership and deadlines

Each Connection owns its descriptor and SSL object. One session handler uses it;
TLS state is not shared across threads. The coordinator shares a read-only TLS
context whose lifetime exceeds every handler. Handshakes run inside the existing
bounded handler slots, not the accept loop or registry lock.

Connect and TLS handshake each have a three-second deadline. Every application
frame preserves its existing absolute header/body deadline across partial TLS
reads and writes. TLS retry conditions poll for the requested read/write readiness
without resetting that deadline. Buffered TLS bytes are read before polling, so
coalesced records cannot stall waiting for an already-drained kernel socket.

The CLI ignores SIGPIPE to survive a closed TLS peer; spawned commands explicitly
restore default SIGPIPE behavior. Connection destruction frees TLS state and
closes TCP without waiting for a TLS close-notify exchange. A close or truncated
frame is an error; TLS close is not a successful job-completion marker. Jobs still
need an entire validated RESULT frame. No automatic retry is introduced.

See the [OpenSSL nonblocking guide](https://docs.openssl.org/3.2/man7/ossl-guide-tls-client-non-block/)
for the WANT_READ/WANT_WRITE retry contract used by the transport.

## Security limits and evidence

A trusted client certificate grants access to all coordinator operations. Worker
IDs remain session labels and are not bound to certificate subjects. There are no
per-peer roles, certificate revocation checks, or automatic certificate renewal.
Workloads are trusted and retain the worker's privileges and inherited environment,
including credential file paths. A workload running as the same user can read that
user's keys. TLS protects transit, not the worker host from a submitted command.

CTest covers mutual authentication, wrong CA, missing/untrusted client certificates,
wrong IP SAN, certificate purpose, malformed configuration, handshake and frame
deadlines, two-worker execution, binary bounded output, timeouts, ps/logs, token
admission within TLS, and disconnection. The Docker bridge script adds distinct
network namespaces and non-loopback IP routing on one host. These are correctness
tests, not performance measurements or a hostile-code security evaluation.

Validation on November 3, 2026: GCC 13.3 Debug with OpenSSL 3.0.13 on Ubuntu 24.04
passed all seven CTest suites (54.50 seconds). The final binary separately passed
the four-container Docker bridge test. GitHub Actions is configured for both;
local results do not establish the remote workflow's status.

### Separate physical machines: pending verification

1. Record commit, compiler, Linux versions, machine names, and private IPs.
2. Issue certificates with the coordinator's real reachable IP SAN and distribute
   only the necessary peer credentials through a trusted channel.
3. Run the coordinator on one machine and workers on different Linux machines.
4. Run commands printing hostname and architecture; confirm results originate on
   eligible workers. Use concurrent jobs to exercise both workers.
5. Verify wrong trust, missing client credentials, and mismatched IP SAN are rejected.
6. Inspect jobs/logs, run a timeout, and disconnect a worker during an active job.
   Confirm LOST without retries, then verify remaining/replacement capacity.
7. Save actual commands and results before claiming physical-machine execution.
