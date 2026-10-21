# proxrs

A Rust CLI that exposes a loopback HTTP proxy through password-authenticated SSH. The SSH server opens destination connections and resolves their names; no proxy software, sudo, or server configuration changes are required.

## Current workflow

Start the proxy from a terminal on macOS with an ordinary Ubuntu or Debian SSH server:

```sh
cargo run -- up --host server.example --user alice
```

For an unknown host, inspect the host, port, key type, and SHA-256 fingerprint before typing `yes`. Approval appends the key to `~/.ssh/known_hosts` by default; `--known-hosts PATH` selects another file. The password prompt reads from the controlling terminal with echo disabled. Prompt time does not consume the shared 10-second SSH network and authentication deadline.

For automation, establish host trust first and supply the password through stdin:

```sh
secret-provider read ssh-password | cargo run -- up \
  --host server.example --user alice --password-stdin \
  --known-hosts /path/to/trusted_known_hosts
```

`--ssh-port` defaults to 22. The local proxy binds only `127.0.0.1`, at `--port 8080` by default; `--port 0` chooses an available port. An occupied requested port is an error. The password is never an argument and is not saved. Ordinary and hashed known-host records, nondefault ports, wildcard and negative hostname patterns, and multiple host keys are supported. A matching revocation overrides a trusted key. Changed keys, malformed or unreadable storage, and relevant certificate-authority rules fail closed; SSH host certificates are unsupported. Unknown hosts require explicit approval through the controlling terminal, including when the password comes from stdin. Headless automation cannot enroll a key. Unrelated trust records are preserved, and a failure to save approved trust stops authentication.

The proxy forwards ordinary HTTP/1.1 requests and streams uploads and responses through SSH. HTTPS and secure WebSocket traffic use opaque CONNECT tunnels.

After authentication and listener binding, `up` prints six shell exports. Apply them in a second shell while proxrs remains running. Both HTTP/HTTPS variable variants use an `http://127.0.0.1:<port>` URL; HTTPS uses an opaque CONNECT tunnel with the application's own TLS verification. Proxy exclusions default to loopback only. These variables configure cooperating clients; exclusions and applications that ignore proxy variables can connect directly.

Ctrl-C or SIGTERM stops the foreground proxy and its tunnels. Transient SSH failures use at most three retries, delayed by 1, 2, and 4 seconds. Each attempt has a shared 10-second network/authentication budget excluding human prompt time. Startup and subsequent short-lived connections share the retry budget; 60 continuous seconds of readiness restores all three retries. Wrong passwords, unsupported authentication, host-trust failures, and protocol or policy rejection stop immediately.

During recovery, interrupted HTTP requests and tunnels fail, the same listener returns 503 for new requests, and the next authenticated SSH connection serves new work. Requests are never replayed or sent directly by proxrs. The password is retained in zeroizing memory for the session and released on shutdown. SSH keepalives run every 15 seconds; three unanswered probes detect a silent connection loss at approximately 60 seconds. Healthy quiet tunnels remain open. Exhausted retries close the listener and exit nonzero.

## Managed commands

Launch a foreground command with arguments after `--`:

```sh
cargo run -- run --host server.example --user alice -- curl https://example.com
secret-provider read ssh-password | cargo run -- run \
  --host server.example --user alice --password-stdin -- your-command --your-option
```

`run` waits for authentication and listener readiness, then launches the command directly with literal arguments. It sets upper/lowercase HTTP and HTTPS proxy variables to the actual loopback URL, removes `ALL_PROXY`/`all_proxy`, and replaces both exclusion variables with `localhost,127.0.0.1,::1`. Use `--inherit-no-proxy` before `--` to append these defaults to inherited exclusions. A set lowercase `no_proxy` wins over uppercase, even when empty; an inherited `*` is preserved exactly. Only the child's environment changes.

Child stdout/stderr remain available, and `run` does not print export instructions. Hidden password entry preserves terminal input. With `--password-stdin`, the command receives a fresh blocking controlling-terminal descriptor when available, otherwise null input; it never receives the password pipe. The command owns terminal Ctrl-C, so a command that handles it and continues keeps its proxy. Proxrs restores the prior foreground process group and terminal settings on exit, including launch errors.

Normal command completion closes the proxy, cleans remaining members of the command's process group, and returns the command's status. The command remains running during SSH retries and can use the proxy again after recovery. Directed SIGINT/SIGTERM or exhausted retries close the proxy and terminate the owned group; command completion cancels pending retries. Cleanup allows five seconds before SIGKILL. Unrelated processes and already running services are never owned; deliberately detached daemons are outside this process-group guarantee. A signal-ended command returns `128 + signal`.

## Development

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
bash scripts/docker-ssh-smoke.sh
cargo build
python3 tests/docker/proxy_cases.py --image ubuntu:24.04
python3 tests/docker/proxy_cases.py --image debian:12
python3 tests/docker/trust_cases.py --image ubuntu:24.04
python3 tests/docker/trust_cases.py --image debian:12
python3 tests/docker/run_cases.py --image ubuntu:24.04
python3 tests/docker/run_cases.py --image debian:12
python3 tests/docker/http_cases.py --image ubuntu:24.04
python3 tests/docker/http_cases.py --image debian:12
python3 tests/docker/cleanup_cases.py --image ubuntu:24.04
python3 tests/docker/cleanup_cases.py --image debian:12
python3 tests/docker/recovery_cases.py --image ubuntu:24.04
python3 tests/docker/recovery_cases.py --image debian:12
cargo build --release
python3 tests/docker/load_cases.py --image ubuntu:24.04
python3 tests/docker/load_cases.py --image debian:12
```

The SSH transport uses maintained russh with an explicit, pinned [receive-window and authentication-result patch](vendor/russh/PROXRS_PATCH.md) to keep stalled channels from blocking healthy ones. Hyper handles HTTP and CONNECT; a bounded httparse ingress pass rejects ambiguous framing before Hyper normalizes headers. See the [HTTP acceptance report](tests/docker/HTTP_ACCEPTANCE.md) for protocol cases and bounds, and the [assembled acceptance report](tests/docker/ACCEPTANCE.md) for recovery, lifecycle, and final release RSS measurements.

Default Rust tests drive CLI validation, port collision, and cancellation without Docker, credentials, or internet services. The smoke script validates the OpenSSH fixture. The Python cases exercise the actual proxrs binary through real SSH and controlled origins. See [fixture details](tests/docker/README.md) for prerequisites, isolation, and the acceptance coverage.
