# OrbStack SSH fixture

Run from macOS with OrbStack running and its `orbstack` Docker context available:

```sh
bash scripts/orbstack-ssh-smoke.sh
```

Requires Docker CLI, OpenSSH client, OpenSSL, and standard macOS shell utilities. The first run downloads Ubuntu 24.04 and installs OpenSSH/Python packages in the image; later runs reuse Docker's build cache. It leaves the tagged test image cached for reuse.

The script starts a fresh container with only its SSH port published to a random macOS loopback port. It generates a disposable password for the non-root `proxrs` user and fresh server host keys. It obtains the host public key through Docker and verifies it using a temporary known-hosts file. The host's SSH configuration and known-hosts files are not changed.

Checks:

1. Authenticate from the macOS OpenSSH client with a password; key and keyboard-interactive authentication are disabled.
2. Open a `direct-tcpip` channel using `ssh -W` and receive an HTTP response from an origin bound to the container's loopback interface. The origin port is not published.
3. Reject an incorrect password.
4. Remove the owned container and temporary credentials on exit, including failures and normal interruption.

OpenSSH receives the generated fixture password through a temporary `SSH_ASKPASS` helper. The fixture grants no sudo privileges: the intended proxy path needs only password authentication and TCP forwarding.

This is an environment smoke test. It does not test proxrs, HTTPS CONNECT, remote DNS, reconnection, or managed commands yet. Extend it with controlled HTTP/HTTPS origins and the Rust CLI as those features are implemented. Add a Debian fixture before claiming the release supports both server distributions. The full required cases live in [SPEC.md](../../SPEC.md#acceptance-criteria).

OrbStack's built-in SSH access for Linux machines accepts key authentication only. This Docker fixture runs its own OpenSSH server so it can exercise the selected password-first workflow. [OrbStack SSH documentation](https://docs.orbstack.dev/machines/ssh)
