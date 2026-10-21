# Docker SSH fixture

Run from macOS with a local Docker engine available through the Docker CLI:

```sh
bash scripts/docker-ssh-smoke.sh
```

Requires Docker CLI, OpenSSH client, OpenSSL, and standard macOS shell utilities. The script uses the active Docker configuration, including context and environment overrides, without selecting a runtime provider. The engine must publish container ports to this Mac's loopback interface; a remote Docker engine is not supported by this local fixture. The first run downloads Ubuntu 24.04 and installs OpenSSH/Python packages in the image; later runs reuse Docker's build cache. It leaves the tagged test image cached for reuse.

The script starts a fresh container with only its SSH port published to a random macOS loopback port. It generates a disposable password for the non-root `proxrs` user and fresh server host keys. It obtains the host public key through Docker and verifies it using a temporary known-hosts file. The host's SSH configuration and known-hosts files are not changed.

Checks:

1. Authenticate from the macOS OpenSSH client with a password; key and keyboard-interactive authentication are disabled.
2. Open a `direct-tcpip` channel using `ssh -W` and receive an HTTP response from an origin bound to the container's loopback interface. The origin port is not published.
3. Reject an incorrect password.
4. Remove the owned container and temporary credentials on exit, including failures and normal interruption.

OpenSSH receives the generated fixture password through a temporary `SSH_ASKPASS` helper. The fixture grants no sudo privileges: the intended proxy path needs only password authentication and TCP forwarding.

This is an environment smoke test. It does not test proxrs, HTTPS CONNECT, remote DNS, reconnection, or managed commands. The real-binary CONNECT suite below adds Ubuntu and Debian coverage. The full required cases live in [SPEC.md](../../SPEC.md#acceptance-criteria).

The fixture runs its own OpenSSH server inside Docker so it can exercise the selected password-first workflow independently of the host's SSH setup.

## Real proxrs CONNECT cases

The opt-in binary suite needs Python 3 in addition to the smoke-test dependencies:

```sh
cargo build
python3 tests/docker/proxy_cases.py --image ubuntu:24.04
python3 tests/docker/proxy_cases.py --image debian:12
```

Each run builds the chosen distribution, creates its own container, password, keys, and test CA, and removes owned processes, containers, and temporary files even after an assertion failure. Only SSH is published on macOS loopback. HTTP, TLS, echo, flood, and stalled-connect origins remain unpublished inside the container. `origin.proxrs.test` exists only in the container's hosts file. The test client trusts only the disposable CA and performs strict certificate validation. The IPv6 SSH-address case uses a byte-for-byte local `::1` relay to this same real SSH server, because Docker's published IPv6 availability depends on the local runtime configuration.

The suite covers CONNECT/TLS, remote DNS, IPv6 destination authorities, 16 concurrent requests, half-close, malformed authorities, refused destinations, forwarding denial without SSH shutdown, host-key rejection, unsupported authentication, wrong passwords, RSA/SHA2 host keys, and a stalled PAM authentication response with a 10-second deadline per attempt and the bounded retry budget. Passwords include leading/trailing spaces and arrive with CRLF to verify line-terminator-only removal. It also checks emitted exports, chosen ports, loopback binding with `lsof`, SIGINT/SIGTERM, established SSH-loss cancellation, 503 responses and recovery, slow-reader isolation, and a healthy destination progressing while another destination's connect is stalled and returns504.

The existing `docker-ssh-smoke.sh` remains a separate fixture check; it does not establish Rust proxy correctness. Ordinary HTTP streaming and the release RSS workload have a separate suite below. Managed commands and full recovery/failure injection have separate suites below. Do not infer those acceptance results from this CONNECT suite.

## Real proxrs terminal and trust cases

```sh
cargo build
python3 tests/docker/trust_cases.py --image ubuntu:24.04
python3 tests/docker/trust_cases.py --image debian:12
```

This suite imports the same isolated fixture and runs the macOS binary with a real controlling pseudo-terminal. It checks hidden password entry (including whitespace), exact terminal-state restoration on success/error/cancellation, Ctrl-C and SIGTERM while either prompt is waiting without a newline, first-key fingerprint display, explicit/blank/declined approval, saved-key reuse, and HTTPS after enrollment. Password piping and terminal approval use separate descriptors. A real waiting shell proves cancellation discards a partially typed password, while successful input preserves the following terminal line. Human delays longer than ten seconds at each prompt must still succeed. Newly created trust files and directories have private permissions; existing records and comments remain intact.

The public CLI trust matrix covers OpenSSH-generated hashed hostnames, nondefault ports, wildcard and negative hostname patterns, whitespace, multiple key algorithms and record ordering, matching and unrelated revocations, changed keys, malformed/unreadable storage, persistence failure, and relevant versus unrelated certificate-authority records. A real SSH server restricted to a host-certificate algorithm must report the unsupported certificate requirement. A temporary PAM helper in the owned container counts authentication attempts without reading or recording credentials, proving trust rejection happens before password authentication. The fixture restores PAM and SSH configuration and removes its container and generated files on exit. It never changes the developer's SSH configuration.

Host certificates and CA-based trust are explicitly unsupported. The suite validates ordinary host-key trust only; managed-child terminal ownership is a separate acceptance suite.

## Real proxrs HTTP and load cases

```sh
cargo build
python3 tests/docker/http_cases.py --image ubuntu:24.04
python3 tests/docker/http_cases.py --image debian:12
cargo build --release
python3 tests/docker/load_cases.py --image ubuntu:24.04
python3 tests/docker/load_cases.py --image debian:12
```

These suites use the same isolated fixture. They cover origin-form routing, Host and hop-header handling, reused client sockets across destinations, malformed framing and header deadlines, streamed request/response bodies, secure WebSocket traffic, cancellation, channel/client capacity, and healthy requests beside slow readers. The macOS release load runner compares lifetime peak RSS for 16 concurrent streams transferring 16 MiB and 1 GiB in aggregate. See [HTTP_ACCEPTANCE.md](HTTP_ACCEPTANCE.md) for the exact workload, measured results, buffer bounds, and verified acceptance cases. No private module tests or public origins are involved.

## Real proxrs managed-command cases

```sh
cargo build
python3 tests/docker/run_cases.py --image ubuntu:24.04
python3 tests/docker/run_cases.py --image debian:12
```

The suite launches a controlled Python foreground command through the actual Rust binary. It verifies literal arguments, validated HTTPS through the unpublished origin, the complete proxy-exclusion precedence matrix, all four proxy variables, ALL_PROXY removal, headless null stdin despite leftover password-pipe bytes, nonzero exit propagation, and listener shutdown. Invalid authentication and occupied ports prevent launch.

Real macOS pseudo-terminals cover fresh blocking input after hidden and piped passwords, queued command input after the password, foreground ownership before the child's first read, Ctrl-C handled by a continuing command followed by HTTPS, and launch failure/startup cancellation/directed termination. An existing foreground helper verifies hand-back of its process group, exact terminal settings, and its next input line after the child changes canonical/echo settings. Darwin's transient PENDIN queue flag is allowed only until that next input operation; the final settings comparison is exact.

Cooperative and SIGTERM-ignoring commands/descendants exercise normal completion, directed SIGINT/SIGTERM, and real SSH loss. Tests verify the listener closes during the five-second grace, owned processes disappear, and an unrelated sentinel survives. Each test owns fresh processes and removes them in finally blocks; deliberately detached daemons are outside the product guarantee and are not created by this suite. These are proxrs acceptance checks, separate from the OpenSSH-only smoke script. Persistent SSH loss keeps the command alive during the 1/2/4-second retries, then applies the same five-second shutdown grace on exhaustion.


## Real proxrs recovery cases

```sh
cargo build
python3 tests/docker/recovery_cases.py --image ubuntu:24.04
python3 tests/docker/recovery_cases.py --image debian:12
```

An opaque loopback TCP relay interrupts only this fixture's SSH connection while preserving its actual server host keys and origin state. It has bounded directional buffers, connection count, and control queue. Tests inject immediate closure, a stalled handshake, and silent loss without TCP EOF. They observe real connection attempts and wall-clock delays, public HTTP status/streams, origin side effects, PAM authentication counts, process status, and terminal behavior.

Coverage includes startup and established recovery, 1/2/4 delays, four ten-second stalled attempts, shared budgets through flaps, a full 61-second stable reset, healthy quiet tunnels, three unanswered 15-second keepalives, HTTP client socket reuse after recovery, broken-stream cancellation, prompt 503, and no replay of a counted POST. Authentication transport loss reuses the retained password; an explicit rejection with MaxAuthTries=1 remains terminal. Changed/revoked/unknown/unreadable trust on reconnect is rejected before another password attempt. Managed cases verify startup launch gating, the same child and foreground Ctrl-C behavior through recovery, restoration after exhaustion, child exit/directed signals during retry delay or handshake, listener/group cleanup, and unrelated process survival. See [ACCEPTANCE.md](ACCEPTANCE.md) for final commands and observed measurements.
