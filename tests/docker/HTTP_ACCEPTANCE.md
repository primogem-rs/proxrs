# HTTP and streaming acceptance

Ticket [#4](https://github.com/primogem-rs/proxrs/issues/4) uses the actual proxrs executable, its loopback HTTP endpoint, stdout/stderr, and controlled origins behind a real OpenSSH server. The tests import the disposable `Fixture` from `proxy_cases.py`. Build images are identified through `docker build --iidfile` and launched by immutable image ID, so concurrent runs cannot select each other's tagged image.

## Reproduce

Run from macOS with the prerequisites in [README.md](README.md):

```sh
cargo build
python3 tests/docker/http_cases.py --image ubuntu:24.04
python3 tests/docker/http_cases.py --image debian:12
python3 tests/docker/proxy_cases.py --image ubuntu:24.04
python3 tests/docker/proxy_cases.py --image debian:12
cargo build --release
python3 tests/docker/load_cases.py --image ubuntu:24.04 --output /tmp/proxrs-load-ubuntu.json
python3 tests/docker/load_cases.py --image debian:12 --output /tmp/proxrs-load-debian.json
```

The Rust default tests do not invoke Docker or public services. `scripts/docker-ssh-smoke.sh` checks the OpenSSH fixture separately; it does not establish proxrs correctness. Every Python suite creates and removes only its own processes, container, password, keys, known-hosts file, and test CA. Only the SSH port is published. Destination DNS exists only in the container. HTTPS and secure WebSocket clients validate the origin certificate using their own disposable CA trust; proxrs does not terminate TLS.

## Verified behavior

`http_cases.py` exercises these public behaviors on Ubuntu 24.04 and Debian 12:

- Ordinary explicit-proxy HTTP reaches unpublished origins through SSH. The origin sees the path/query in origin form and Host from the URI authority. One actual client socket alternates between ports 8080 and 8081. Lowercase `http_proxy` works with curl.
- Request and response hop-by-hop headers, fields named by Connection, and proxy credentials are stripped. Origin Authorization survives forwarding. Authorization values and request bodies remain absent from proxrs logs.
- The first event-stream line arrives in less than 1.1 seconds, while completion takes at least two seconds. Separate probes observe 4096 uploaded bytes before fixed-length and chunked uploads finish their 8192 bytes.
- Invalid target/Host/port/IPv6 syntax, bare line feeds, conflicting Content-Length, Transfer-Encoding plus Content-Length, and invalid chunk sizes return 400. Explicit HTTP ports that are empty, nonnumeric, or above 65535 do not fall back to port 80; text after a closing IPv6 bracket is rejected for both HTTP and CONNECT. A missing HTTP port reaches a controlled port-80 origin, and valid IPv6 HTTP/CONNECT still succeed. Heads above 32 KiB return 431. An incomplete head returns 504 after approximately ten seconds.
- A malformed pipelined request gets its response after the preceding event stream. A later malformed body cannot change an earlier upstream failure from 502 to 400. An origin truncation after response headers ends the stream without appending another HTTP response.
- A response that pauses for eleven seconds preserves reuse of the same client socket. The first header deadline starts on accept; subsequent deadlines start with the next request's first byte. Quiet established responses and tunnels have no idle deadline.
- CONNECT failures close the client connection. Successful CONNECT preserves payload coalesced with the header, bidirectional traffic, and half-close. A WebSocket exchanges independent server and masked client messages inside certificate-validated TLS.
- Repeatedly abandoning 140 responses that each declare 1 GiB releases channel capacity. All 128 active channels count toward the channel limit; a pending destination open consumes the same capacity. Excess work gets 503 promptly. All 256 accepted clients, including CONNECT tunnels, count toward the client limit; excess sockets close promptly. Released work restores capacity after channel cleanup.
- Refused destinations and denied SSH forwarding return 502 without terminating the authenticated SSH session. Their diagnostics distinguish destination connection failure from policy denial, which points to AllowTcpForwarding and PermitOpen.

`proxy_cases.py` additionally verifies the existing CONNECT slice: port boundaries 1 and 65535 are accepted as destinations, malformed ports and IPv6 authorities are rejected, loopback binding is inspected with `lsof`, uppercase/lowercase exports use the actual bound port, concurrent TLS requests progress beside stalled readers and a stalled destination open, and SIGINT/SIGTERM close listeners and tunnels. SSH loss closes old tunnels, returns 503 during recovery, and allows fresh requests after reconnection. That suite checks password/host-trust failures and the ten-second authentication deadline per attempt too.

## Bounds and cancellation

Hyper owns HTTP semantics, body decoding, and response serialization. A bounded ingress pass uses maintained `httparse` before Hyper can normalize ambiguous framing. It preserves framing bytes while forwarding bodies incrementally. Header errors travel through an eight-entry status queue to a valid internal placeholder request so Hyper orders errors after earlier responses; the error status never travels in a client-controlled header. The bridge uses a 16 KiB duplex buffer and the ingress reader uses 8 KiB. Heads, chunk-size lines, and trailers each have a 32 KiB bound. Hyper's maximum request head is also 32 KiB.

Each destination stream uses the existing 16 KiB duplex buffer, 64 KiB SSH receive window, and 16 KiB maximum SSH packet. The pinned [russh receive-window patch](../../vendor/russh/PROXRS_PATCH.md) returns receive credit only as data is consumed. Dropping an HTTP response body aborts its HTTP connection driver, which drops the destination stream and cancels the channel relay. Channel permits remain held through the actual SSH close, including pending opens and canceled work. Accepted-client permits survive CONNECT upgrade until the tunnel ends. No request is retried or sent directly.

## Release RSS measurement

Recorded after integrating #3 at `a5c5bdc`, on macOS 27.0.1, Darwin 27.0.0 arm64, Rust/Cargo 1.98.1. Each workload starts a fresh release-build proxrs process. Sixteen readers start together, and a second barrier waits for all sixteen response headers before any reader drains a body. Each reader requests one sixteenth of the aggregate payload, reads 16 KiB, and sleeps 1 ms after each read. Thus each stream transfers 1 MiB in the small run and 64 MiB in the large run. The origin generates fixed 16 KiB blocks rather than allocating a complete payload.

An independent client makes ordinary HTTP requests throughout each transfer, pausing 50 ms between probes. Every probe must finish within three seconds. `os.wait4` records each proxrs child's lifetime peak RSS in macOS bytes after clean termination; the runner does not subtract cumulative child-process maxima. The assertion compares the larger process's peak RSS with the smaller process's peak and requires a delta no greater than 67,108,864 bytes (64 MiB).

| SSH fixture | Aggregate bytes | Peak RSS bytes | Elapsed seconds | Healthy requests |
| --- | ---: | ---: | ---: | ---: |
| Ubuntu 24.04 | 16,777,216 | 12,648,448 | 1.348 | 23 |
| Ubuntu 24.04 | 1,073,741,824 | 13,975,552 | 5.958 | 90 |
| Debian 12 | 16,777,216 | 11,075,584 | 1.163 | 20 |
| Debian 12 | 1,073,741,824 | 14,155,776 | 6.247 | 113 |

Ubuntu's delta was 1,327,104 bytes (1.27 MiB); Debian's was 3,080,192 bytes (2.94 MiB). Both passed the 64 MiB acceptance threshold while healthy requests continued to complete. These are observed fixture measurements, not an estimate or a throughput promise.

These #4 measurements are historical slice evidence. The final assembled build, including managed-command and bounded SSH recovery suites, is recorded in [ACCEPTANCE.md](ACCEPTANCE.md) with new release RSS measurements on both distributions. No suite establishes application-specific compatibility.
