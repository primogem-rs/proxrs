# Assembled v0.1 acceptance

Recorded on macOS 27.0.1 (Darwin 27.0.0, arm64), Rust/Cargo 1.98.1, with the assembled #2–#6 implementation. These checks use the actual macOS proxrs executable, real Ubuntu 24.04 and Debian 12 OpenSSH servers, unpublished container-local origins, and fresh disposable credentials/trust. Application-specific compatibility, production services, and public Internet destinations are outside this acceptance boundary.

## Commands and results

The final validation runs use:

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo build
cargo build --release
cargo test --manifest-path vendor/russh/Cargo.toml --no-default-features --features ring,rsa --lib
bash scripts/docker-ssh-smoke.sh
```

The six default public CLI tests pass without Docker, credentials, or Internet access. The full vendored library suite passes 192 tests. The dependency retains its pre-existing compression-disabled unused-constant warning; proxrs passes strict Clippy. The explicit vendor patch applies to the documented pristine russh release and reproduces all vendored source files byte for byte.

The OpenSSH smoke script separately passes password login, direct-tcpip HTTP, wrong-password refusal, and fixture teardown against Ubuntu. It does **not** exercise proxrs. Actual proxrs acceptance uses these suites on **both** distributions:

```sh
python3 -u tests/docker/recovery_cases.py --image ubuntu:24.04
python3 -u tests/docker/recovery_cases.py --image debian:12
python3 -u tests/docker/proxy_cases.py --image ubuntu:24.04
python3 -u tests/docker/proxy_cases.py --image debian:12
python3 -u tests/docker/trust_cases.py --image ubuntu:24.04
python3 -u tests/docker/trust_cases.py --image debian:12
python3 -u tests/docker/http_cases.py --image ubuntu:24.04
python3 -u tests/docker/http_cases.py --image debian:12
python3 -u tests/docker/run_cases.py --image ubuntu:24.04
python3 -u tests/docker/run_cases.py --image debian:12
python3 -u tests/docker/load_cases.py --image ubuntu:24.04 --output /tmp/proxrs-load-ubuntu.json
python3 -u tests/docker/load_cases.py --image debian:12 --output /tmp/proxrs-load-debian.json
```

All listed actual-binary suites passed. The recovery suite's final two added cases—startup retry budget carrying into an established outage and password rejection during reconnect—also passed separately on both distributions against the same executable; they are included in the runner above for future complete runs.

## Recovery observations

`recovery_cases.py` puts an opaque TCP relay between proxrs and the real OpenSSH server. Its active connections, directional buffers, and control queue are bounded. It can reject new connections, stall handshakes, close established connections, or discard bytes without sending EOF. The SSH host keys and origin counter survive these transport failures. Tests observe attempt timestamps, actual HTTP/CONNECT streams, process status, PAM counts, and controlled child/terminal behavior.

| Observation | Ubuntu 24.04 | Debian 12 |
| --- | ---: | ---: |
| Persistent immediate startup failure: initial plus three attempts | 7.04 s | 7.03 s |
| Observed retry gaps | 1.00 / 2.01 / 4.01 s | 1.00 / 2.01 / 4.01 s |
| Four stalled startup attempts, including retry delays | 47.04 s | 47.04 s |
| Observed stalled-attempt gaps | 11.01 / 12.01 / 14.01 s | 11.01 / 12.01 / 14.01 s |
| Quiet healthy tunnel before full budget reset | 61 s, then echo succeeds | 61 s, then echo succeeds |
| Silent transport loss, no TCP EOF or application probe | 60.01 s | 60.01 s |

A transient startup failure recovers after the first one-second delay without premature exports or child launch. That startup retry consumes the same budget as later outages: the next persistent outage has only the two- and four-second retries. Repeated brief recoveries consume the remaining budget; after three retries, another loss exits without opening another SSH connection. A full 61-second ready interval restores all three delays. This is measured wall-clock behavior against real SSH, not a simulated timer test.

On established loss, an old CONNECT stream closes without HTTP text, and a partially delivered HTTP response ends without a second response. New HTTP and CONNECT requests receive 503 promptly while the listener keeps its port. Fresh certificate-validated HTTPS succeeds after recovery; an already accepted, idle HTTP client uses its same socket successfully with the new SSH connection. A counted POST produces exactly one origin side effect before and after recovery, proving that interrupted requests are not replayed. The container-only DNS name and unpublished origin also rule out successful direct fallback.

A real transport drop while PAM is processing the password retries and authenticates using the retained password without another input line. Explicit wrong-password rejection stays terminal, including OpenSSH configured with MaxAuthTries=1, which closes the connection. A changed remote password during reconnect also stops after one new authentication attempt. Changed, revoked, unknown headless, or unreadable host trust prevents another password attempt. The driver uses typed disconnect/error reasons; arbitrary server descriptions and `is_closed()` guesses do not decide authentication outcomes. The [russh patch report](../../vendor/russh/PROXRS_PATCH.md) explains the narrow upstream correction and regression test.

Directed SIGINT/SIGTERM cancels startup retry delays and stalled handshakes promptly. Managed startup waits for readiness before launching once. During an established outage, the same foreground child remains alive and handles terminal Ctrl-C; after recovery it performs real HTTPS. Exhaustion restores the previous foreground process and terminal settings. Child completion during a retry returns its status, cancels further attempts, closes the listener, and cleans remaining group members. Directed termination cancels pending delay or handshake as well. Cooperative and SIGTERM-ignoring groups verify the five-second shutdown grace after the seven-second retry budget, disappearance of owned processes, listener closure before grace, and survival of an unrelated sentinel.

## Earlier slices on the assembled build

- `proxy_cases.py`: authenticated CONNECT/TLS, remote DNS, IPv6, RSA/SHA2 trust, authority validation, exports, loopback binding, 16 concurrent requests, half-close, stalled reader/destination isolation, refusal/policy diagnostics, password/host-trust failures, per-attempt PAM deadline, explicit signals, and old-stream failure followed by recovery. Its earlier immediate-exit loss assertion now checks 503/recovery and still verifies final listener cleanup.
- `trust_cases.py`: real controlling-terminal password/approval prompts, hidden input, long human delays excluded from the network budget, cancellation and exact restoration, hashed/multiple/wildcard/negative host records, revocation, persistence failures, unsupported certificates, and PAM proof that trust rejection precedes credentials.
- `http_cases.py`: ordinary HTTP routing and Host rewrite, socket reuse across destinations, hop-header removal, sensitive-value log absence, incremental fixed/chunked uploads and event streams, strict framing/size/deadline errors, ordered pipelining, truncated-origin cancellation, TLS WebSockets, CONNECT coalescing/half-close, abandoned response cleanup, and channel/client capacity recovery. The [HTTP report](HTTP_ACCEPTANCE.md) describes the workload and buffer bounds.
- `run_cases.py`: literal command arguments, child-only proxy environment and exclusion precedence, null input headlessly, fresh blocking terminal input after password piping, command status, foreground Ctrl-C, startup/launch errors, exact terminal hand-back, and owned process-group cleanup on normal completion, explicit signals, and exhausted retries.

## Final release memory workload

Each workload starts a fresh release-build proxrs process. Sixteen readers synchronize before requests and again after all response headers; each reads 16 KiB and sleeps 1 ms. Aggregate payloads are 16,777,216 bytes and 1,073,741,824 bytes. A separate client issues HTTP requests throughout, with a three-second bound per request and 50 ms pauses. macOS `os.wait4` records that individual proxy process's lifetime peak RSS in bytes after clean termination. The origin generates fixed blocks rather than allocating complete payloads.

| SSH fixture | Aggregate bytes | Peak RSS bytes | Elapsed seconds | Healthy requests |
| --- | ---: | ---: | ---: | ---: |
| Ubuntu 24.04 | 16,777,216 | 11,141,120 | 1.202 | 2 |
| Ubuntu 24.04 | 1,073,741,824 | 13,991,936 | 6.624 | 98 |
| Debian 12 | 16,777,216 | 11,567,104 | 1.222 | 20 |
| Debian 12 | 1,073,741,824 | 14,319,616 | 6.462 | 96 |

Ubuntu's delta is 2,850,816 bytes (2.72 MiB); Debian's is 2,752,512 bytes (2.62 MiB). Both pass the 67,108,864-byte (64 MiB) limit while independent requests continue. These observations supersede the historical #4 measurements for the assembled build and are not a throughput guarantee.

## Scope and cleanup

No known v0.1 acceptance failure remains. The suites generate per-run passwords, host keys, known-hosts files, CA keys, and owned processes/containers; finally blocks remove them after success or failure. They never change developer SSH configuration or reuse unrelated containers. Build images remain cached. Logs are checked for passwords, authorization markers, and request body markers.

Validated client platform is macOS; server fixtures are Ubuntu 24.04 and Debian 12. Ordinary SSH host keys and password authentication are supported. SSH certificates, key/agent/keyboard-interactive authentication, independently running services, detached daemons, Linux/Windows clients, and application-specific compatibility remain outside the agreed v0.1 scope. Best-effort password zeroization does not claim control over all library or operating-system copies.

## Final review regressions

The final two-axis review identified two HTTP authority defects and four standards/maintenance findings. The authority regression was first run against the pre-fix actual binary with fresh controlled origins: overflow, nonnumeric, and empty HTTP ports incorrectly reached port 80, and text after a closing IPv6 bracket was accepted for both HTTP and CONNECT. All five returned 200. The corrected shared validator returns 400 for these forms, keeps CONNECT's explicit-port requirement, defaults only an absent HTTP port to 80, and preserves ordinary HTTP plus certificate-validated CONNECT over valid IPv6.

A separate red regression sent SIGTERM to a fixture-owning harness after its actual proxy completed HTTPS. Its container survived. Shared SIGINT/SIGTERM handling now unwinds suite contexts, ignores repeated termination during that unwind, and restores the previous handlers when done. The green check observes removal of the proxy process, listener, container, and temporary credentials and survival of an unrelated process. A second red regression interrupted startup after the real Docker daemon created a container but before its CLI returned the ID; this also leaked. Shared signal deferral now completes container-ID registration, detached-process registration, and cleanup before delivering termination. Both startup and ready-state interruption cases pass. The regression's own finally block removes only its reported owned resources after a failure, including both deliberate red runs. Diagnostic formatting/emission now resides in the terminal layer; trust verification/enrollment share their known-host identity formatter.

Review validation uses the current macOS binary and fresh Ubuntu 24.04 and Debian 12 fixtures:

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo build
python3 tests/docker/cleanup_cases.py --image ubuntu:24.04
python3 tests/docker/cleanup_cases.py --image debian:12
python3 tests/docker/http_cases.py --image ubuntu:24.04
python3 tests/docker/http_cases.py --image debian:12
python3 tests/docker/trust_cases.py --image ubuntu:24.04
python3 tests/docker/trust_cases.py --image debian:12
python3 tests/docker/proxy_cases.py --image ubuntu:24.04
python3 tests/docker/proxy_cases.py --image debian:12
python3 tests/docker/run_cases.py --image ubuntu:24.04
python3 tests/docker/run_cases.py --image debian:12
bash scripts/docker-ssh-smoke.sh
```

The existing `startup_transient` and `established_recovery` public cases were also run on both distributions through the same fixture, exercising the relocated retry/loss/restoration diagnostics with real SSH. This review changes authority validation and output ownership, not stream buffering or the retry state machine; the complete assembled recovery and release RSS measurements above remain the relevant earlier evidence.

All listed final-review commands passed on both distributions, along with six default Rust tests and the separate fixture-only smoke. The later ownership guard was rechecked with both startup and ready-state interruption cases on both distributions; no production code changed after the full affected-suite runs.
