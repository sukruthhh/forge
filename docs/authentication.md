# Loopback authentication foundation

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
argument. Unset FORGE_TOKEN retains the unauthenticated loopback development mode;
an explicitly empty or malformed value fails rather than disabling authentication.
Clients configured with a token never fall back to an unauthenticated request.

The coordinator validates its configuration before opening a listener, then
requires AUTHENTICATE as the first frame on every connection. Successful
authentication receives OK, followed by the ordinary registration, submission,
or inspection exchange. Failed authentication returns a generic ERROR and closes
the socket before touching the registry or job queue. Authentication and the
following request each have a three-second receive deadline. Existing handler
and frame bounds still apply. Tokens are not included in diagnostics.

## Scope and limits

The token is a bearer credential sent without encryption over loopback. It does
not authenticate the coordinator to clients or prevent replay if captured. All
credential holders have the same permissions, including worker registration and
command submission; worker IDs are session labels, not cryptographic identities.
There is no per-worker revocation or role separation. Rotate the token by stopping
and restarting the coordinator and peers with a new secret.

Commands remain trusted and inherit the worker environment, including FORGE_TOKEN,
and user privileges. This gate is not a hostile-workload boundary. The listener
still binds only 127.0.0.1. Do not expose it through a public port forward or proxy.
Authenticated encrypted transport and separate-machine tests are still required
before claiming native cross-machine support. No such claim follows from these
local tests.

## Wire extension

Protocol v1 adds type 13: AUTHENTICATE, containing exactly one token field.
In token mode the coordinator replies OK or ERROR before an operation is sent.
An unconfigured coordinator rejects AUTHENTICATE; a configured client therefore
fails closed against an older or unconfigured coordinator. Repeated authentication
is invalid. Existing message schemas and unauthenticated development mode remain
unchanged.
