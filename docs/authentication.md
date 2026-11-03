# Optional shared-token admission

Mutual TLS and remote endpoint setup are documented in [network-setup.md](network-setup.md).
FORGE_TOKEN can be used in either TLS mode or plaintext loopback development.

Set the same `FORGE_TOKEN` environment variable on the coordinator, workers, and
CLI clients to enable shared-token authentication. The value must contain 32-256
printable ASCII characters without whitespace. Use a randomly generated secret;
length validation does not establish randomness. Keep it out of Git and logs.

For example, in each Linux terminal, load a protected local credential file:

```sh
export FORGE_TOKEN="$(cat /path/to/private/forge-token)"
./build/recovery/forge coordinator
# In another terminal with the same FORGE_TOKEN:
./build/recovery/forge join --id desktop
# In a third terminal with the same FORGE_TOKEN:
./build/recovery/forge run -- /usr/bin/printf 'hello\n'
```

Protect the file with owner-only permissions. No credential is passed as a CLI
argument. Unset FORGE_TOKEN disables this additional gate; TLS admission remains
active if configured. With both TLS and the token unset, loopback development is
unauthenticated. An explicitly empty or malformed token fails rather than disabling authentication.
Clients configured with a token never fall back to an unauthenticated request.

When FORGE_TOKEN is configured, the coordinator validates it before opening a listener, then
requires AUTHENTICATE as the first frame on every connection. Successful
authentication receives OK, followed by the ordinary registration, submission,
or inspection exchange. Failed authentication returns a generic ERROR and closes
the socket before touching the registry or job queue. Authentication and the
following request each have a three-second receive deadline. Existing handler
and frame bounds still apply. Tokens are not included in diagnostics.

## Scope and limits

In plaintext loopback mode the token is a bearer credential sent without
encryption. The token alone does not authenticate the coordinator or prevent
replay if captured. In TLS mode the token travels inside a mutually authenticated,
encrypted connection. All credential holders have the same permissions, including worker registration and
command submission; worker IDs are session labels, not cryptographic identities.
There is no per-worker revocation or role separation. Rotate the token by stopping
and restarting the coordinator and peers with a new secret.

Commands remain trusted and inherit the worker environment, including FORGE_TOKEN,
and user privileges. This gate is not a hostile-workload boundary. The listener
defaults to 127.0.0.1; non-loopback endpoints require mutual TLS. Do not expose
plaintext loopback through a port forward or proxy. Network execution has been
verified with local processes and separate containers; physical-machine tests
are pending. Neither token authentication nor TLS provides workload isolation.

## Wire extension

Protocol v1 adds type 13: AUTHENTICATE, containing exactly one token field.
In token mode the coordinator replies OK or ERROR before an operation is sent.
An unconfigured coordinator rejects AUTHENTICATE; a configured client therefore
fails closed against an older or unconfigured coordinator. Repeated authentication
is invalid. Existing message schemas and unauthenticated development mode remain
unchanged.
