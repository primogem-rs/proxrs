# proxrs

A Rust CLI that exposes a loopback HTTP proxy through password-authenticated SSH. The SSH server opens destination connections and resolves their names; no proxy software, sudo, or server configuration changes are required.

## Current workflow

The first implementation slice supports scripted HTTPS CONNECT on macOS with Ubuntu and Debian SSH servers. Supply a password through stdin and an isolated or existing trusted ordinary known-hosts file:

```sh
secret-provider read ssh-password | cargo run -- up \
  --host server.example --user alice --password-stdin \
  --known-hosts /path/to/trusted_known_hosts
```

`--ssh-port` defaults to 22. The local proxy binds only `127.0.0.1`, at `--port 8080` by default; `--port 0` chooses an available port. An occupied requested port is an error. The password is never an argument and is not saved. A changed, revoked, unknown, malformed, or unsupported host-trust record fails closed.

After authentication and listener binding, proxrs prints six shell exports. Apply them in a second shell while proxrs remains running. Both HTTP/HTTPS variable variants use an `http://127.0.0.1:<port>` URL; HTTPS uses an opaque CONNECT tunnel with the application's own TLS verification. Proxy exclusions default to loopback only. These variables configure cooperating clients; exclusions and applications that ignore proxy variables can connect directly.

Ctrl-C or SIGTERM stops the foreground proxy and its tunnels. In this slice, SSH loss ends the session with a nonzero status. Interactive enrollment, complete ordinary known-host pattern support, HTTP forwarding, managed commands, and bounded recovery are tracked by the remaining [v0.1 tickets](https://github.com/primogem-rs/proxrs/issues/1); see [SPEC.md](SPEC.md).

## Development

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
bash scripts/docker-ssh-smoke.sh
cargo build
python3 tests/docker/proxy_cases.py --image ubuntu:24.04
python3 tests/docker/proxy_cases.py --image debian:12
```

The SSH transport uses maintained russh with an explicit, pinned [receive-window patch](vendor/russh/PROXRS_PATCH.md) to keep stalled channels from blocking healthy ones. Hyper parses HTTP and CONNECT requests.

Default Rust tests drive CLI validation, port collision, and cancellation without Docker, credentials, or internet services. The smoke script validates the OpenSSH fixture. The Python cases exercise the actual proxrs binary through real SSH and controlled origins. See [fixture details](tests/docker/README.md) for prerequisites, isolation, and the acceptance coverage.
