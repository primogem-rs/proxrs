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

The suite covers CONNECT/TLS, remote DNS, IPv6 destination authorities, 16 concurrent requests, half-close, malformed authorities, refused destinations, forwarding denial without SSH shutdown, host-key rejection, unsupported authentication, wrong passwords, RSA/SHA2 host keys, and a stalled PAM authentication response with the 10-second startup deadline. Passwords include leading/trailing spaces and arrive with CRLF to verify line-terminator-only removal. It also checks emitted exports, chosen ports, loopback binding with `lsof`, SIGINT/SIGTERM, established SSH-loss cleanup, slow-reader isolation, and a healthy destination progressing while another destination's connect is stalled and returns504.

The existing `docker-ssh-smoke.sh` remains a separate fixture check; it does not establish Rust proxy correctness. Complete interactive trust, ordinary HTTP streaming, the release RSS workload, managed-command/PTY behavior, and recovery/failure injection belong to later ticket suites. Do not infer those acceptance results from this CONNECT suite.
