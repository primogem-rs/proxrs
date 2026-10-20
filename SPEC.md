# proxrs v0.1 specification

Status: initial specification, 2026-10-21. The Rust CLI is not implemented yet.

## Purpose

Given SSH connection details, proxrs exposes a local HTTP proxy so applications such as Codex, pi, and other HTTP clients can reach destinations through the SSH server. The user supplies credentials; proxrs establishes and supervises the connection and provides usable proxy environment settings.

## Confirmed scope

- Rust CLI; macOS is the first supported client platform.
- Ubuntu/Debian SSH servers. Root or sudo may be available, but this design does not require either.
- Local proxy over SSH, with proxrs running for the lifetime of the session.
- Username/password authentication first. Hidden interactive entry and `--password-stdin`; credentials are not saved or passed as command arguments.
- Both a standalone foreground proxy and `proxrs run -- <command>`.
- Retry SSH disconnections a few times, then exit on failure.
- Integration validation from macOS against a real SSH server in a Docker container, independent of the local Docker runtime provider.

The remaining numeric defaults below are initial design choices, adjustable through implementation experience.

## Architecture

```text
application -> HTTP proxy on 127.0.0.1 -> encrypted SSH connection -> destination
                     proxrs                    SSH server
```

The HTTP listener runs locally. For each destination connection, proxrs opens an SSH `direct-tcpip` channel. The SSH server resolves destination hostnames and opens outbound connections; only resolving the SSH server's own hostname happens locally. SSH forwarding supports a hostname or an IP address as its destination. [RFC 4254, section 7.2](https://www.rfc-editor.org/rfc/rfc4254.html#section-7.2)

No remote daemon, package installation, shell bootstrap, sudo, firewall changes, or public proxy port is needed. The SSH account must permit password authentication and local TCP forwarding to the requested destinations. If server policy denies forwarding, show the cause and required setting; do not edit server policy automatically. [OpenSSH server configuration](https://man.openbsd.org/sshd_config)

Select the Rust SSH and HTTP libraries during the first implementation slice. Evaluate password authentication, known-hosts verification, concurrent channels, asynchronous streaming, and cancellation together before committing to a library. An HTTP proxy backed by direct SSH channels is the intended design; an additional SOCKS service is unnecessary.

## CLI contract

Planned examples; these commands do not work yet:

```sh
# Prompts for the SSH password; runs until interrupted.
proxrs up --host server.example --user alice

# Choose SSH and local ports explicitly.
proxrs up --host server.example --user alice --ssh-port 2222 --port 8080

# Pass a password from a secret provider through stdin.
secret-provider read ssh-password | proxrs up --host server.example --user alice --password-stdin

# Own the proxy and the application lifetime together.
proxrs run --host server.example --user alice -- codex
proxrs run --host server.example --user alice -- pi
```

Shared options:

| Option | Contract |
| --- | --- |
| `--host <host>` | Required DNS name or IP address; no embedded credentials. |
| `--user <user>` | Required SSH username. |
| `--ssh-port <port>` | Default `22`. |
| `--port <port>` | Default `8080`; `0` requests an OS-assigned local port. An occupied explicit/default port is an error. |
| `--password-stdin` | Read one password line from stdin instead of prompting; remove the line terminator only. |
| `--known-hosts <path>` | Default `~/.ssh/known_hosts`; enables isolated automation and fixtures. |

The listener is IPv4 loopback (`127.0.0.1`) in v1; exposing it to the LAN is out of scope. The SSH server and destinations may use IPv4 or IPv6. Argument values are passed through typed APIs, never interpolated into shell commands.

## Credentials and host trust

1. Validate CLI arguments and obtain the server's SSH host key.
2. Verify the key against known hosts, including non-default SSH ports.
3. On an unknown host in an interactive terminal, display the host, port, key type, and SHA-256 fingerprint and ask whether to trust and save it. Default is refusal. A changed key is an error, with no automatic replacement.
4. Noninteractive operation requires a matching trusted host key already present. There is no insecure verification bypass.
5. Authenticate with the supplied username and password. Prompt through the controlling terminal without echo; absence of a terminal requires `--password-stdin`. Keyboard-interactive/MFA is deferred and reported as unsupported if required by the server.

Retain the password in memory only while the proxy session needs it for reconnection. Avoid unnecessary copies and use best-effort zeroization on shutdown. Never include passwords in URLs, output, telemetry, configuration, or subprocess arguments. With `--password-stdin`, host-key approval still uses the terminal, never the password stream. A managed command receives `/dev/null` as stdin in this mode; ordinary interactive password entry preserves the command's terminal input.

Host-key checks also apply to every reconnection. Authentication rejection and changed host keys are terminal errors, rather than repeated login attempts that could lock an account.

## HTTP and environment behavior

- Accept HTTP/1.1 explicit proxy requests and HTTPS `CONNECT host:port` requests, including bracketed IPv6 authorities.
- Forward ordinary HTTP requests with correct origin-form paths and Host handling. Reject malformed or ambiguous framing using the HTTP library; strip hop-by-hop fields and proxy credentials before forwarding. Handle connection reuse without mixing destinations.
- Establish the destination channel before acknowledging CONNECT. Then relay bytes bidirectionally without inspecting TLS or installing a certificate authority; support TLS negotiation, streaming, WebSocket traffic inside CONNECT, and half-close.
- Forward bodies incrementally with backpressure. Streaming responses must reach the client before the origin closes them. HTTP/2 inside a CONNECT tunnel remains opaque.
- Map malformed requests to `400`, oversized headers to `431`, destination/channel failures to `502`, connection deadlines to `504`, and temporary SSH unavailability to `503`. After a CONNECT success or response headers, close a failed stream instead of injecting a new HTTP response into it.
- Never resolve destination hostnames on macOS or retry a failed request directly. The local proxy has no separate credentials; loopback is its access boundary, including access by other local processes.
- Support TCP destination ports `1..65535`, subject to the SSH server's forwarding policy. UDP, QUIC, a SOCKS listener, and TLS interception are out of scope.

After the listener is bound and SSH authentication has succeeded, `up` prints shell-quoted exports to stdout; prompts, status, and diagnostics go to stderr. Readiness means local listening plus authenticated SSH; destination reachability and forwarding permission are established per request. The terminal instructions explain that the exports belong in a second shell while proxrs remains running.

```sh
export HTTP_PROXY='http://127.0.0.1:8080'
export HTTPS_PROXY='http://127.0.0.1:8080'
export http_proxy='http://127.0.0.1:8080'
export https_proxy='http://127.0.0.1:8080'
export NO_PROXY='localhost,127.0.0.1,::1'
export no_proxy='localhost,127.0.0.1,::1'
```

Use the actual bound port. Both proxy URLs use the `http://` scheme, including `HTTPS_PROXY`: HTTPS destinations use CONNECT through that HTTP proxy. Emit lowercase variables too, since curl deliberately ignores uppercase `HTTP_PROXY`. [curl proxy environment variables](https://github.com/curl/everything-curl/blob/master/usingcurl/proxies/env.md)

`run` starts its command only after readiness, without a shell wrapper, sets the same variables in the child's environment, and keeps child stdout/stderr available to the terminal. Existing proxy variables are overridden for this child only. Preserve existing `NO_PROXY`/`no_proxy` exclusions and append the loopback defaults; if both are set differently, lowercase takes precedence and the resulting value is assigned to both. Remove inherited `ALL_PROXY`/`all_proxy` for the managed command. Never modify the parent shell or its profile files.

Proxy environment variables are application configuration, not network enforcement. Applications that ignore them can still connect directly, and explicit exclusions bypass the proxy. Compatibility must be demonstrated per application; do not claim that arbitrary programs are forced through SSH.

## Session lifecycle and defaults

State transitions:

```text
connecting -> ready -> reconnecting -> ready
     |           |          |
     +-----------+----------+-> stopping -> exited
```

| Setting | Initial default |
| --- | --- |
| SSH connection/handshake deadline | 10 seconds per attempt; excludes time spent at interactive prompts |
| Destination channel connection deadline | 10 seconds |
| HTTP request header deadline / maximum size | 10 seconds / 32 KiB |
| SSH keepalive | Every 15 seconds; consider the connection lost after 3 unanswered probes |
| Reconnection | 3 attempts per outage, after delays of 1, 2, and 4 seconds |
| Retry reset | Reset the outage budget after 60 seconds of stable SSH connectivity; brief flapping consumes the same budget |
| Concurrent destination channels | 128; reject excess work promptly with `503` |
| Established stream idle timeout | None by default; rely on cancellation and SSH liveness to preserve quiet streams |
| Managed command shutdown grace | 5 seconds before forced termination |

Retry transient connection failures during startup under the same bounded policy. Fail immediately for invalid configuration, authentication rejection, changed host keys, or a local bind error. A single destination refusal does not reconnect a healthy SSH session.

On an established SSH connection loss, close affected streams and report the outage. Keep the listener bound and return `503` for new requests while reconnecting; do not accumulate requests or replay partially completed ones. The managed command remains running during retries. After the retry budget is exhausted, stop accepting requests, terminate the managed command and its owned descendants, and exit nonzero.

On Ctrl-C or termination, close the listener and channels, cancel retries, release credentials, and shut down the managed command. Preserve normal terminal interaction for interactive commands; signal only owned child processes. When the managed command finishes normally, close the proxy and return its exit status. Proxy/authentication failures produce a nonzero status and an actionable stderr message; exact numeric error categories can be settled during CLI implementation.

## Acceptance criteria

The release is ready only when these pass against the actual Rust binary:

1. Password login on fresh Ubuntu and Debian fixtures; unknown-host approval, a trusted key, and isolated known-hosts automation work. Wrong passwords, changed host keys, and unsupported auth fail clearly without leaking credentials.
2. Both HTTP forwarding and HTTPS CONNECT reach controlled origins through SSH. A destination resolvable only within the container network proves remote DNS; an unpublished origin proves the path. A test CA is trusted only by the HTTPS test client, whose certificate validation remains enabled.
3. Uppercase and lowercase environment settings work as specified, including preserved exclusions and overridden inherited proxy settings. Use curl with lowercase `http_proxy` for the HTTP case.
4. A slow streaming origin delivers early chunks before completion; a large response stays within a bounded memory budget. Concurrent requests do not block unrelated channels. Malformed input and excessive headers fail predictably.
5. Refused destinations, denied forwarding, channel limits, port collisions, and IPv6 destination syntax have clear outcomes. Listener inspection confirms loopback binding.
6. Interrupt the SSH connection during traffic. Verify broken streams are not replayed, new requests fail during reconnection, recovery serves new requests, and flapping/exhaustion terminates within the documented budget.
7. `run` forwards command arguments literally, supplies the correct child environment, preserves interactive terminal behavior, propagates completion, and cleans up owned processes on interruption or exhausted reconnects.
8. Teardown leaves no listener, child process, test container, or credential file behind. Logs never contain passwords, authorization headers, or request bodies.
9. Manual smoke checks with the user's installed Codex and pi verify a real request and streaming behavior. Record the client versions and any application-specific configuration; do not infer support from curl alone.

Use deterministic container-local origins for the core suite, with no production credentials or billable API calls. Test the real CLI and local HTTP endpoint, observing standard streams, environment output, exit status, and managed-command behavior rather than private module internals. The initial Docker harness validates password SSH and direct forwarding only; see [fixture instructions](tests/docker/README.md). Application checks are a later, separate acceptance step.

## Implementation sequence

1. Authenticate with password, verify host keys, and relay one HTTPS CONNECT request over a real SSH channel in Docker.
2. Add HTTP forwarding, streamed bodies, limits, environment output, and `up` lifecycle handling.
3. Add `run`, child environment/terminal handling, exit propagation, and cleanup.
4. Add bounded reconnect supervision and failure injection; complete the acceptance matrix on Ubuntu and Debian and smoke-test the intended applications.

Deferred: key/agent authentication, `~/.ssh/config` aliases and jump hosts, keyboard-interactive authentication, Linux/Windows client support, persistent remote proxies, daemon mode, public listeners, saved profiles, and a system-wide VPN.
