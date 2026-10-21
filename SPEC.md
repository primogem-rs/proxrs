# proxrs v0.1 specification

Status: implemented v0.1 contract, 2026-10-21. The Rust CLI and bounded recovery are covered by the [assembled acceptance report](tests/docker/ACCEPTANCE.md). Implementation is tracked in [issue #1](https://github.com/primogem-rs/proxrs/issues/1).

## Purpose

Given SSH connection details, proxrs exposes a local HTTP proxy for applications that honor proxy environment settings. The user supplies credentials; proxrs establishes and supervises the connection and provides usable proxy environment settings. Codex and pi are example consumers. The product contract is generic HTTP forwarding and HTTPS CONNECT, without application-specific adapters or compatibility certification.

## Confirmed scope

- Rust CLI; macOS is the first supported client platform.
- Ubuntu/Debian SSH servers. Root or sudo may be available, but this design does not require either.
- Local proxy over SSH, with proxrs running for the lifetime of the session.
- Username/password authentication first. Hidden interactive entry and `--password-stdin`; credentials are not saved or passed as command arguments.
- Both a standalone foreground proxy and `proxrs run -- <command>`.
- `run` manages a foreground command and its process group; deliberately detached daemons are outside its cleanup guarantee.
- Ordinary SSH host keys; SSH host certificates and certificate-authority trust are deferred.
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

Examples (build with `cargo build --release` and use `target/release/proxrs`):

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

Options:

| Option | Contract |
| --- | --- |
| `--host <host>` | Required DNS name or IP address; no embedded credentials. |
| `--user <user>` | Required SSH username. |
| `--ssh-port <port>` | Default `22`. |
| `--port <port>` | Default `8080`; `0` requests an OS-assigned local port. An occupied explicit/default port is an error. |
| `--password-stdin` | Read one password line from stdin instead of prompting; remove the line terminator only. |
| `--known-hosts <path>` | Default `~/.ssh/known_hosts`; enables isolated automation and fixtures. |
| `--inherit-no-proxy` | `run` only: explicitly inherit proxy exclusions from the environment. By default, only loopback destinations are excluded. |

The listener is IPv4 loopback (`127.0.0.1`) in v1; exposing it to the LAN is out of scope. The SSH server and destinations may use IPv4 or IPv6. Argument values are passed through typed APIs, never interpolated into shell commands.

## Local installation

- `proxrs install` installs the executable being invoked into a user-owned bin directory. The intended workflow starts with `cargo build --release` and `./target/release/proxrs install`.
- The default location is `~/.local/bin/proxrs`; `--bin-dir <directory>` overrides the bin directory. Create the directory when needed without invoking sudo.
- An install directory containing `:` cannot be represented in PATH and is rejected, including when resolving a directory symlink reveals a colon.
- Install an independent copy of the invoked binary, so moving or deleting the checkout and running `cargo clean` do not break the installed command.
- Accept both release and debug builds; release compilation is the documented workflow rather than an installation restriction.
- If the destination already contains `proxrs`, prompt before replacing it even when its contents are identical, defaulting to No. `--force` skips the prompt. Without an interactive terminal, replacement fails with instructions to use `--force`. Declining leaves the existing binary untouched and exits successfully.
- Installation does not build or download a binary, require SSH connection details, or start a proxy session.
- Installation never modifies the parent shell or shell profile files. If PATH setup is needed, it prints the setup command for the user to apply.
- Installation succeeds even if the install directory is missing from PATH or another `proxrs` takes precedence. Report the installed path and any remaining shell setup separately, and print a command to prepend the install directory when needed.
- Select the user's shell automatically where possible, using a maintained library when suitable, without adding a shell-selection option. Print setup instructions for `sh`, `bash`, `zsh`, and `fish`. If shell detection is unavailable or the shell is unsupported, still install successfully and report the installed path and the detection or support limitation without guessing a setup command.
- Printed PATH commands affect the current shell session. Explain how the user can save the command in their shell's startup configuration for future sessions; proxrs does not edit that configuration.
- Print setup commands to stdout and status, prompts, and diagnostics to stderr.

```sh
cargo build --release
./target/release/proxrs install
./target/release/proxrs install --bin-dir /chosen/bin
./target/release/proxrs install --force
```

Validate installation with the actual CLI in temporary directories and isolated HOME/PATH environments, without SSH or Docker:

1. Default and custom locations receive an executable independent copy, usable after removing its source copy. No SSH arguments, credentials, or network access are needed.
2. Existing destinations prompt even for identical contents. Approval and `--force` allow replacement; No or an empty answer preserves the destination and exits successfully. Noninteractive replacement without `--force` fails clearly.
3. Missing PATH entries and an earlier competing `proxrs` produce actionable setup instructions without failing installation. Supported-shell commands safely handle directories containing spaces, apostrophes, and backslashes. Unknown or unavailable shell detection does not produce guessed syntax.
4. Shell startup configuration remains untouched. A failed copy must preserve any existing executable and leave no partial installed binary.

## Credentials and host trust

1. Validate CLI arguments and obtain the server's SSH host key.
2. Verify the key against known hosts, including non-default SSH ports.
3. On an unknown host in an interactive terminal, display the host, port, key type, and SHA-256 fingerprint and ask whether to trust and save it. Default is refusal. A changed key is an error, with no automatic replacement.
4. Noninteractive operation requires a matching trusted host key already present. There is no insecure verification bypass.
5. Authenticate with the supplied username and password. Prompt through the controlling terminal without echo; absence of a terminal requires `--password-stdin`. Keyboard-interactive/MFA is deferred and reported as unsupported if required by the server.

Retain the password in memory only while the proxy session needs it for reconnection. Avoid unnecessary copies and use best-effort zeroization on shutdown. Never include passwords in URLs, output, telemetry, configuration, or subprocess arguments. With `--password-stdin`, host-key approval still uses the terminal, never the password stream. Give the managed command the controlling terminal as stdin when available; use `/dev/null` only when no controlling terminal exists. Never pass the password stream to the managed command. Ordinary interactive password entry preserves the command's terminal input.

Host-key checks also apply to every reconnection. Authentication rejection and changed host keys are terminal errors, rather than repeated login attempts that could lock an account.

Support ordinary known-host entries, hashed hostnames, non-default ports, and multiple valid keys for a host. Evaluate all applicable records: a matching revoked key is rejected even if another record trusts it; a different recorded key must not hide a valid matching key. Report unreadable or malformed trust storage as an error, rather than treating it as an unknown host. SSH host certificates and certificate-authority trust are unsupported in v1; fail clearly when required, and never silently ignore relevant unsupported trust rules.

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

`run` starts its command only after readiness, without a shell wrapper, sets the same variables in the child's environment, and keeps child stdout/stderr available to the terminal. Existing proxy variables are overridden for this child only. By default, replace inherited `NO_PROXY`/`no_proxy` values with the loopback exclusions shown above. With `--inherit-no-proxy`, preserve the inherited exclusions and append loopback defaults; a set lowercase value takes precedence over uppercase, and the resulting value is assigned to both. Preserve an inherited bypass-all value of `*` exactly rather than appending entries, since clients differ in how they interpret a wildcard inside a list. Remove inherited `ALL_PROXY`/`all_proxy` for the managed command. Never modify the parent shell or its profile files.

Proxy environment variables configure cooperating applications. Applications that ignore them can still connect directly, and explicit exclusions bypass the proxy. Proxrs sets generic environment variables and relays traffic; it does not detect application identities, alter their launch arguments, manage their authentication, or configure existing background services. Application-specific networking remains the application's responsibility.

## Session lifecycle and defaults

State transitions:

```text
connecting -> ready -> reconnecting -> ready
     |           |          |
     +-----------+----------+-> stopping -> exited
```

| Setting | Initial default |
| --- | --- |
| SSH startup deadline | 10 seconds per attempt across connection, handshake, and authentication responses; excludes time spent at interactive prompts |
| Destination channel connection deadline | 10 seconds |
| HTTP request header deadline / maximum size | 10 seconds / 32 KiB |
| SSH keepalive | Every 15 seconds; consider the connection lost after 3 unanswered probes |
| Reconnection | 3 attempts per outage, after delays of 1, 2, and 4 seconds |
| Retry reset | Reset the outage budget after 60 seconds of stable SSH connectivity; brief flapping consumes the same budget |
| Accepted client connections | 256; promptly close excess connections before allocating request buffers |
| Concurrent destination channels | 128, including pending opens; reject excess work promptly with `503` |
| Established stream idle timeout | None by default; rely on cancellation and SSH liveness to preserve quiet streams |
| Managed command shutdown grace | 5 seconds before forced termination |

Retry transient connection failures during startup under the same bounded policy. Fail immediately for invalid configuration, authentication rejection, changed host keys, or a local bind error. A single destination refusal does not reconnect a healthy SSH session.

Bound HTTP buffers, SSH windows, and pending queues as well as connection counts. Backpressure must reach the destination when clients read slowly; the implementation must not accumulate complete response bodies in memory.

On an established SSH connection loss, close affected streams and report the outage. Keep the listener bound and return `503` for new requests while reconnecting; do not accumulate requests or replay partially completed ones. The managed command remains running during retries. After the retry budget is exhausted, stop accepting requests, terminate the managed command's process group, and exit nonzero. Cleanup covers processes that remain in that group; deliberately detached daemons are outside the guarantee. Proxrs does not attach to or take ownership of already running application services.

In standalone `up`, and during startup before a managed command owns the terminal, Ctrl-C stops the proxy session. Once a managed command owns the terminal, its Ctrl-C behavior belongs to that command: keep the proxy running if the command handles the input or signal and continues, and stop the proxy when the command exits. Preserve normal terminal interaction and restore terminal ownership on exit. Explicit termination directed at proxrs closes the listener and channels, cancels retries, releases credentials, and shuts down the managed process group; never signal unrelated processes. When the managed command finishes normally, close the proxy, clean up remaining processes in its group, and return the command's exit status. Proxy/authentication failures produce a nonzero status and an actionable stderr message; exact numeric error categories can be settled during CLI implementation.

## Acceptance criteria

The release is ready only when these pass against the actual Rust binary:

1. Password login on fresh Ubuntu and Debian fixtures; unknown-host approval, a trusted key, and isolated known-hosts automation work. Cover hashed entries, multiple valid keys, non-default ports, revocations, unreadable/malformed trust files, and unsupported host certificates. Wrong passwords, changed host keys, unsupported auth, and stalled authentication responses fail clearly within the relevant deadlines without leaking credentials.
2. Both HTTP forwarding and HTTPS CONNECT reach controlled origins through SSH. A destination resolvable only within the container network proves remote DNS; an unpublished origin proves the path. A test CA is trusted only by the HTTPS test client, whose certificate validation remains enabled.
3. Uppercase and lowercase environment settings work as specified. Verify loopback-only exclusions by default even when the inherited value is `*`; explicit inheritance preserves exclusions, wildcard semantics, and lowercase precedence. Use curl with lowercase `http_proxy` for the HTTP case.
4. Controlled origins verify HTTP event-stream chunks arriving before completion and bidirectional WebSocket traffic through HTTPS CONNECT, with TLS validation enabled. Exercise slow readers and compare release-build peak RSS at 16 concurrent streams transferring 16 MiB versus 1 GiB total: the larger workload may add at most 64 MiB of peak RSS. Record the workload and measurements. Healthy concurrent requests must continue making progress while other clients are stalled. Malformed input and excessive headers fail predictably.
5. Refused destinations, denied forwarding, channel limits, port collisions, and IPv6 destination syntax have clear outcomes. Listener inspection confirms loopback binding.
6. Interrupt the SSH connection during traffic while preserving the SSH server's host keys. Verify broken streams are not replayed, new requests fail during reconnection, recovery serves new requests, and flapping/exhaustion terminates within the documented budget. Changed-host-key rejection is a separate test.
7. `run` forwards command arguments literally, supplies the correct child environment, and preserves interactive terminal behavior, including when the SSH password is piped in. Without a controlling terminal, password-from-stdin mode gives the child null input. Verify that a child handling Ctrl-C can continue using the proxy, that child completion closes it and propagates the exit status, and that explicit termination of proxrs or exhausted reconnects cleans up the managed process group. Verify terminal ownership is restored on exit.
8. Teardown leaves no proxy listener, member of the managed process group, test container, or credential file behind. Unrelated processes must remain untouched. Any deliberately detached processes created by a test are cleaned up by the test harness. Logs never contain passwords, authorization headers, or request bodies.

Use deterministic container-local origins and generic HTTP clients, with no production credentials or billable API calls. Test the real CLI and local HTTP endpoint, observing standard streams, environment output, exit status, and a controlled managed command's behavior rather than private module internals. The separate smoke harness validates password SSH and direct forwarding only; actual-binary suites cover the assembled contract. See [fixture instructions](tests/docker/README.md) and the [acceptance report](tests/docker/ACCEPTANCE.md). Specific applications, providers, login flows, and token refresh are not release acceptance targets.

## Completed implementation sequence

1. Authenticate with password, verify host keys, and relay one HTTPS CONNECT request over a real SSH channel in Docker.
2. Add HTTP forwarding, streamed bodies, limits, environment output, and `up` lifecycle handling.
3. Add `run`, child environment/terminal handling, exit propagation, and cleanup.
4. Add bounded reconnect supervision and failure injection; complete the protocol and lifecycle acceptance matrix on Ubuntu and Debian.

Deferred: key/agent authentication, SSH host certificates and certificate-authority trust, `~/.ssh/config` aliases and jump hosts, keyboard-interactive authentication, Linux/Windows client support, persistent remote proxies, daemon mode, public listeners, saved profiles, and a system-wide VPN. Application-specific adapters, provider/authentication integrations, compatibility certification, and management of independent daemons are outside this project's v1 scope.
