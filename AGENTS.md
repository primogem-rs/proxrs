# Project guidance

proxrs is a Rust CLI that exposes a local HTTP proxy through an authenticated SSH server.

## Context

- Read [SPEC.md](SPEC.md) before changing CLI behavior, SSH authentication, proxy handling, reconnection, or process lifecycle. It owns the product contract and acceptance criteria.
- Read [GLOSSARY.md](GLOSSARY.md) when naming domain concepts or changing their meaning; update it when a term is settled.
- Read [tests/docker/README.md](tests/docker/README.md) before running or extending the SSH integration fixture.

## Implementation

- Keep CLI parsing and terminal output separate from SSH transport, HTTP handling, and session supervision. Add modules when a working slice needs them.
- Reuse maintained protocol libraries for SSH and HTTP parsing. Keep remote destination resolution on the SSH server.
- Keep the listener on loopback, verify SSH host keys before sending credentials, and keep passwords out of arguments, logs, and saved configuration.
- Keep HTTP bodies and CONNECT tunnels streaming with bounded buffers. Test streaming behavior and cancellation, not just completed responses.
- Make retries operate on the SSH connection. Never replay an application request after connection loss or fall back to direct internet access.
- Target ordinary SSH forwarding without remote installation, sudo, service changes, or firewall changes.

## Validation

- For Rust changes, run `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test`.
- Keep default Rust tests deterministic and independent of Docker, credentials, and public internet services.
- For changes to SSH, forwarding, or lifecycle behavior, run the relevant integration cases against a disposable Docker container. Exercise real SSH from macOS; a mocked transport alone is insufficient.
- The initial fixture check is `bash scripts/docker-ssh-smoke.sh`. Extend the harness to invoke the Rust CLI when it exists; fixture checks alone do not establish proxy correctness.
- Generate fixture credentials per run, use an isolated known-hosts file, and remove owned containers and temporary credentials on exit. Never reuse unrelated containers or change the developer's SSH configuration.
- Report separately what passed against the fixture, what passed against proxrs, and which acceptance cases remain unverified.

## Agent skills

### Issue tracker

Work is tracked in GitHub Issues for `primogem-rs/proxrs`. Read `docs/agents/issue-tracker.md` before issue-tracker operations.

### Triage labels

Use the five default triage labels. Read `docs/agents/triage-labels.md` before assigning triage states.

### Domain docs

Use a single-context layout: root `GLOSSARY.md` and `docs/adr/`. Read `docs/agents/domain.md` before exploring domain concepts or architectural decisions.
