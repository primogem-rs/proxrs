# proxrs

A planned Rust CLI that turns SSH access into a local HTTP proxy for Codex, pi, and other applications using proxy environment variables.

The initial design supports a macOS client, password authentication to Ubuntu/Debian SSH servers, foreground use, managed application launching, and bounded reconnection. It needs SSH TCP forwarding and does not install software on the server.

## Project status

The project currently contains the Cargo starter, [v0.1 specification](SPEC.md), [agent guidance](AGENTS.md), and a working Docker SSH test fixture. The Rust executable still prints `Hello, world!`; proxy commands are not implemented.

## Development

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
bash scripts/docker-ssh-smoke.sh
```

The last command builds a disposable Ubuntu SSH container using the active Docker configuration, verifies password login and forwarding, then removes it and its temporary credentials. It tests the environment, not the future Rust implementation. See [fixture details](tests/docker/README.md).
