# Pinned russh and the proxrs receive-window patch

This directory is the crates.io release of **russh 0.64.1**, from upstream commit
`e5d80a07dc554480b6ea3b2e14adfc190407b627`. The source retains its upstream
copyright headers. `LICENSE.txt` is the Apache-2.0 license from that exact
upstream commit:
https://github.com/Eugeny/russh/blob/e5d80a07dc554480b6ea3b2e14adfc190407b627/LICENSE.txt

`consumption-window.patch` records the changes relative to the unmodified release.
This is a local patch, not an upstream release or an upstream-approved change.

## Why this is needed

Upstream replenishes a channel's receive window before delivering data into its
bounded receiver queue. If a client stops reading, that queue eventually fills
and blocks the single SSH receive loop, including healthy channels and liveness
processing. A larger queue or smaller window only postpones that failure.

The opt-in `client::Config::direct_tcpip_receive_window_backpressure` flag keeps
ordinary upstream behavior unchanged by default. For opted-in direct-tcpip
channels it replenishes only bytes actually removed by `wait()` or copied by the
async reader. Credits use an atomic counter and shared notification, avoiding an
unbounded queue of per-read acknowledgements. Proxrs uses a 64 KiB receive window
and 16 KiB maximum packet size.

The receive queue has `window_size + 4` message slots so even one-byte packets can
consume the entire permitted byte window without blocking other channels. Empty
data messages and irrelevant control notifications are discarded; EOF and close
notifications are not duplicated. This is a finite queue, not an unbounded
channel. The worst-case message overhead remains proportional to the configured
byte window.

The patch also closes late confirmations after canceled opens, releases local
channel state on close, and avoids accumulating unrelated authentication replies
for direct-tcpip failures. These paths keep failed requests and repeated
cancellation from retaining session state indefinitely.

## Verification and maintenance

Six focused upstream tests exercise exact partial-read credits, empty/control
packet suppression, bounded one-byte packet delivery, independent stalled
channels, and late-open cleanup. The real proxrs suite validates four stopped
readers alongside sixteen successful HTTPS requests, and a stalled destination
open alongside healthy requests before and after its 10-second timeout, against
Ubuntu and Debian OpenSSH. See `tests/docker/proxy_cases.py` and its README.

When updating russh, rebase this explicit patch, review the credit/queue invariants,
and rerun both the focused tests and the real-binary concurrency regressions.
Remove the patch only after an upstream version provides equivalent behavior.
