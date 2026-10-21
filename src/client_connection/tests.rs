use super::{Destinations, SelectedDestination, run};
use crate::transport::DestinationError;
use std::time::Duration;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader, DuplexStream},
    sync::{mpsc, oneshot, watch},
};

#[derive(Clone)]
struct Scripted(watch::Receiver<Option<mpsc::UnboundedSender<Opening>>>);

struct Selected(mpsc::UnboundedSender<Opening>);
struct Opening {
    host: String,
    port: u16,
    reply: oneshot::Sender<Result<DuplexStream, DestinationError>>,
}

impl Destinations for Scripted {
    type Selected = Selected;

    fn select(&self) -> Option<Selected> {
        self.0.borrow().clone().map(Selected)
    }
}

impl SelectedDestination for Selected {
    type Stream = DuplexStream;

    async fn open(self, host: String, port: u16) -> Result<DuplexStream, DestinationError> {
        let (reply, result) = oneshot::channel();
        self.0
            .send(Opening { host, port, reply })
            .map_err(|_| DestinationError::Unavailable)?;
        result.await.unwrap_or(Err(DestinationError::Unavailable))
    }
}

impl Opening {
    fn accept(self, host: &str, port: u16) -> BufReader<DuplexStream> {
        assert_eq!((self.host.as_str(), self.port), (host, port));
        let (stream, peer) = tokio::io::duplex(1024);
        self.reply.send(Ok(stream)).unwrap();
        BufReader::new(peer)
    }
}

async fn head(stream: &mut BufReader<DuplexStream>) -> String {
    let mut head = String::new();
    loop {
        let count = stream.read_line(&mut head).await.unwrap();
        assert_ne!(count, 0, "EOF before a complete head: {head:?}");
        if head.ends_with("\r\n\r\n") {
            return head;
        }
    }
}

#[tokio::test(start_paused = true)]
async fn dropping_runner_closes_a_destination_waiting_for_response_headers() {
    bounded(async {
        let (opens, mut opened) = mpsc::unbounded_channel();
        let (_available, current) = watch::channel(Some(opens));
        let (mut client, stream) = tokio::io::duplex(1024);
        let mut runner = Box::pin(run(stream, Scripted(current)));
        let mut origin = tokio::select! {
            _ = &mut runner => panic!("runner exited before the response"),
            origin = async {
                client.write_all(b"GET http://one.test/path HTTP/1.1\r\nHost: ignored\r\n\r\n").await.unwrap();
                let mut origin = opened.recv().await.unwrap().accept("one.test", 80);
                assert!(head(&mut origin).await.starts_with("GET /path HTTP/1.1\r\n"));
                origin
            } => origin,
        };
        drop(runner);
        // No scheduler turn may be needed to close local streams: task abortion
        // or cancellation of a detached driver would only close them later.
        assert!(matches!(futures_util::poll!(std::pin::pin!(origin.read(&mut [0]))), std::task::Poll::Ready(Ok(0))));
        assert!(matches!(futures_util::poll!(std::pin::pin!(client.read(&mut [0]))), std::task::Poll::Ready(Ok(0))));
    }).await;
}

struct Client {
    io: BufReader<DuplexStream>,
    available: watch::Sender<Option<mpsc::UnboundedSender<Opening>>>,
    opened: mpsc::UnboundedReceiver<Opening>,
    runner: tokio::task::JoinHandle<()>,
}
impl Client {
    fn new() -> Self {
        let (opens, opened) = mpsc::unbounded_channel();
        let (available, current) = watch::channel(Some(opens));
        let (client, stream) = tokio::io::duplex(1024);
        Self {
            io: BufReader::new(client),
            available,
            opened,
            runner: tokio::spawn(run(stream, Scripted(current))),
        }
    }

    async fn send(&mut self, bytes: &[u8]) {
        self.io.write_all(bytes).await.unwrap();
    }

    async fn origin(&mut self, host: &str, port: u16) -> BufReader<DuplexStream> {
        self.opened
            .recv()
            .await
            .expect("destination open")
            .accept(host, port)
    }

    async fn status(&mut self, status: u16) -> String {
        let response = head(&mut self.io).await;
        assert!(
            response.starts_with(&format!("HTTP/1.1 {status} ")),
            "{response}"
        );
        response
    }

    async fn finish(&mut self) {
        self.runner.abort();
        assert!((&mut self.runner).await.unwrap_err().is_cancelled());
    }
}
impl Drop for Client {
    fn drop(&mut self) {
        self.runner.abort();
    }
}

async fn bounded(test: impl std::future::Future<Output = ()>) {
    tokio::time::timeout(Duration::from_secs(300), test)
        .await
        .expect("byte-stream test stalled");
}

#[tokio::test(start_paused = true)]
async fn preflight_then_availability_then_destination_validation_determine_errors() {
    bounded(async {
        for available in [false, true] {
            for (request, expected) in [
                (&b"GET http://one.test/ HTTP/1.1\nHost: x\n\n"[..], 400),
                (&b"GET http://one.test/ HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n"[..], 400),
                (&b"POST http://one.test/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n"[..], 400),
                (&b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"[..], if available {400} else {503}),
                (&b"CONNECT one.test HTTP/1.1\r\nHost: x\r\n\r\n"[..], if available {400} else {503}),
            ] {
                let mut client = Client::new();
                if !available { client.available.send_replace(None); }
                client.send(request).await;
                let response = client.status(expected).await;
                assert!(response.to_lowercase().contains("connection: close\r\n"));
                assert_eq!(client.io.read(&mut [0]).await.unwrap(), 0);
                assert!(client.opened.try_recv().is_err(), "invalid request opened a destination");
            }
        }
    }).await;
}

#[tokio::test(start_paused = true)]
async fn connect_waits_for_open_relays_coalesced_bytes_and_keeps_running_through_half_close() {
    bounded(async {
        let mut client = Client::new();
        client
            .send(b"CONNECT [::1]:443 HTTP/1.1\r\nHost: x\r\n\r\ncoalesced")
            .await;
        let opening = client.opened.recv().await.unwrap();
        assert!(
            futures_util::poll!(std::pin::pin!(client.io.fill_buf())).is_pending(),
            "CONNECT acknowledged before open"
        );
        let mut origin = opening.accept("::1", 443);
        client.status(200).await;
        let mut bytes = [0; 9];
        origin.read_exact(&mut bytes).await.unwrap();
        assert_eq!(&bytes, b"coalesced");
        client.io.shutdown().await.unwrap();
        assert_eq!(origin.read(&mut [0]).await.unwrap(), 0);
        assert!(
            !client.runner.is_finished(),
            "runner returned before the tunnel finished"
        );
        origin.write_all(b"reply-after-eof").await.unwrap();
        origin.shutdown().await.unwrap();
        let mut reply = Vec::new();
        client.io.read_to_end(&mut reply).await.unwrap();
        assert_eq!(reply, b"reply-after-eof");
        (&mut client.runner).await.unwrap();
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn an_open_stays_on_its_selection_and_a_reused_client_selects_the_replacement() {
    bounded(async {
        let mut client = Client::new();
        client
            .send(b"GET http://old.test/a HTTP/1.1\r\nHost: x\r\n\r\n")
            .await;
        let old = client.opened.recv().await.unwrap();
        client.available.send_replace(None);
        let (next, mut replacement) = mpsc::unbounded_channel();
        client.available.send_replace(Some(next));
        let mut origin = old.accept("old.test", 80);
        assert!(head(&mut origin).await.starts_with("GET /a HTTP/1.1\r\n"));
        origin
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nold")
            .await
            .unwrap();
        client.status(200).await;
        let mut body = [0; 3];
        client.io.read_exact(&mut body).await.unwrap();
        assert_eq!(&body, b"old");
        assert!(
            replacement.try_recv().is_err(),
            "started request migrated to replacement"
        );
        client
            .send(b"GET http://new.test:8081/b HTTP/1.1\r\nHost: x\r\n\r\n")
            .await;
        let mut origin = replacement.recv().await.unwrap().accept("new.test", 8081);
        assert!(head(&mut origin).await.starts_with("GET /b HTTP/1.1\r\n"));
        origin
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nnew")
            .await
            .unwrap();
        client.status(200).await;
        client.io.read_exact(&mut body).await.unwrap();
        assert_eq!(&body, b"new");
        assert!(client.opened.try_recv().is_err());
        assert!(replacement.try_recv().is_err());
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn destination_failures_are_mapped_once_without_replaying_on_a_replacement() {
    bounded(async {
        for method in ["GET http://one.test/", "CONNECT one.test:80"] {
            for (failure, expected) in [
                (DestinationError::Unavailable, 503),
                (DestinationError::Capacity, 503),
                (DestinationError::Timeout, 504),
                (DestinationError::Refused, 502),
                (DestinationError::ForwardingDenied, 502),
                (DestinationError::ChannelRejected, 502),
            ] {
                let mut client = Client::new();
                client
                    .send(format!("{method} HTTP/1.1\r\nHost: x\r\n\r\n").as_bytes())
                    .await;
                let opening = client.opened.recv().await.unwrap();
                let (next, mut replacement) = mpsc::unbounded_channel();
                client.available.send_replace(Some(next));
                opening.reply.send(Err(failure)).unwrap();
                client.status(expected).await;
                assert_eq!(client.io.read(&mut [0]).await.unwrap(), 0);
                assert!(client.opened.try_recv().is_err());
                assert!(
                    replacement.try_recv().is_err(),
                    "failed request was replayed"
                );
            }
        }
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn reused_connections_rewrite_routing_and_strip_hop_headers_in_both_directions() {
    bounded(async {
        let mut client = Client::new();
        for (authority, host, port) in [("one.test:8080", "one.test", 8080), ("[::1]", "::1", 80)] {
            client.send(format!("GET http://{authority}/path?query=yes HTTP/1.1\r\nHost: ignored\r\nConnection: X-Remove\r\nConnection: Keep-Alive\r\nX-Remove: private-hop\r\nKeep-Alive: timeout=5\r\nProxy-Authorization: secret\r\nProxy-Connection: keep-alive\r\nAuthorization: end-to-end\r\n\r\n").as_bytes()).await;
            let mut origin = client.origin(host, port).await;
            let request = head(&mut origin).await.to_lowercase();
            assert!(request.starts_with("get /path?query=yes http/1.1\r\n"));
            assert!(request.contains(&format!("\r\nhost: {authority}\r\n")));
            assert!(request.contains("\r\nauthorization: end-to-end\r\n"));
            for forbidden in ["ignored", "x-remove", "private-hop", "keep-alive", "proxy-", "secret"] {
                assert!(!request.contains(forbidden), "{request}");
            }
            origin.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: X-Hop\r\nX-Hop: remove\r\nProxy-Authenticate: remove\r\nX-End: preserve\r\n\r\nhello").await.unwrap();
            let response = client.status(200).await.to_lowercase();
            assert!(response.contains("x-end: preserve\r\n"));
            assert!(!response.contains("x-hop"));
            assert!(!response.contains("proxy-authenticate"));
            let mut body = [0; 5];
            client.io.read_exact(&mut body).await.unwrap();
            assert_eq!(&body, b"hello");
            assert_eq!(origin.read(&mut [0]).await.unwrap(), 0, "finished response kept destination alive");
        }
    }).await;
}

#[tokio::test(start_paused = true)]
async fn a_pipelined_preflight_error_follows_the_complete_streamed_response() {
    bounded(async {
        let mut client = Client::new();
        client.send(b"GET http://one.test/events HTTP/1.1\r\nHost: x\r\n\r\nPOST http://two.test/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n").await;
        let mut origin = client.origin("one.test", 80).await;
        head(&mut origin).await;
        origin.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nfirst").await.unwrap();
        client.status(200).await;
        let mut first = [0; 5];
        client.io.read_exact(&mut first).await.unwrap();
        assert_eq!(&first, b"first");
        assert!(futures_util::poll!(std::pin::pin!(client.io.fill_buf())).is_pending());
        origin.write_all(b"later").await.unwrap();
        client.io.read_exact(&mut first).await.unwrap();
        assert_eq!(&first, b"later");
        client.status(400).await;
        assert_eq!(client.io.read(&mut [0]).await.unwrap(), 0);
        assert!(client.opened.try_recv().is_err(), "malformed pipelined request reached a destination");
    }).await;
}

#[tokio::test(start_paused = true)]
async fn malformed_body_state_belongs_only_to_its_request() {
    bounded(async {
        for first in [false, true] {
            let mut client = Client::new();
            if first {
                client.send(b"GET http://one.test/no-response HTTP/1.1\r\nHost: x\r\n\r\n").await;
            }
            client.send(b"POST http://one.test/upload HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\ninvalid-chunk\r\n").await;
            let mut origin = client.origin("one.test", 80).await;
            if first {
                head(&mut origin).await;
                drop(origin);
            }
            client.status(if first {502} else {400}).await;
            assert_eq!(client.io.read(&mut [0]).await.unwrap(), 0);
            assert!(client.opened.try_recv().is_err());
        }
    }).await;
}

#[tokio::test(start_paused = true)]
async fn header_limits_and_deadlines_do_not_expire_quiet_responses_or_reused_connections() {
    bounded(async {
        for partial in [&b""[..], &b"GET http://one.test/ HTTP/1.1\r\nHost:"[..]] {
            let mut client = Client::new();
            let start = tokio::time::Instant::now();
            client.send(partial).await;
            client.status(504).await;
            assert_eq!(start.elapsed(), Duration::from_secs(10));
            assert!(client.opened.try_recv().is_err());
        }
        let mut oversized = Client::new();
        let mut request = b"GET http://one.test/ HTTP/1.1\r\nHost: x\r\nLarge: ".to_vec();
        request.resize(32768, b'x');
        oversized.send(&request).await;
        oversized.status(431).await;
        assert!(oversized.opened.try_recv().is_err());

        let mut client = Client::new();
        client
            .send(b"GET http://one.test/events HTTP/1.1\r\nHost: x\r\n\r\n")
            .await;
        let mut origin = client.origin("one.test", 80).await;
        head(&mut origin).await;
        origin
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nfirst")
            .await
            .unwrap();
        client.status(200).await;
        let mut body = [0; 5];
        client.io.read_exact(&mut body).await.unwrap();
        assert_eq!(&body, b"first");
        tokio::time::advance(Duration::from_secs(60)).await;
        origin.write_all(b"later").await.unwrap();
        client.io.read_exact(&mut body).await.unwrap();
        assert_eq!(&body, b"later");
        tokio::time::advance(Duration::from_secs(60)).await;
        assert!(futures_util::poll!(std::pin::pin!(client.io.fill_buf())).is_pending());
        client.send(b"GET ").await;
        let start = tokio::time::Instant::now();
        client.status(504).await;
        assert_eq!(start.elapsed(), Duration::from_secs(10));
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn fixed_and_chunked_uploads_arrive_before_completion_and_cancel_with_the_runner() {
    bounded(async {
        for chunked in [false, true] {
            let mut client = Client::new();
            let framing = if chunked {
                "Transfer-Encoding: chunked"
            } else {
                "Content-Length: 10"
            };
            client
                .send(
                    format!("POST http://one.test/upload HTTP/1.1\r\nHost: x\r\n{framing}\r\n\r\n")
                        .as_bytes(),
                )
                .await;
            let mut origin = client.origin("one.test", 80).await;
            let request = head(&mut origin).await.to_lowercase();
            assert!(request.contains(&framing.to_lowercase()));
            client
                .send(if chunked { b"5\r\nfirst\r\n" } else { b"first" })
                .await;
            let mut bytes = vec![0; if chunked { 10 } else { 5 }];
            origin.read_exact(&mut bytes).await.unwrap();
            assert_eq!(
                bytes,
                if chunked {
                    &b"5\r\nfirst\r\n"[..]
                } else {
                    &b"first"[..]
                }
            );
            // The destination observed a prefix while the upload was unfinished.
            client.finish().await;
            let mut remaining = Vec::new();
            origin.read_to_end(&mut remaining).await.unwrap();
            assert!(
                remaining.is_empty(),
                "cancelled upload acquired more body bytes"
            );
            assert_eq!(client.io.read(&mut [0]).await.unwrap(), 0);
        }
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn slow_readers_apply_backpressure_and_cancellation_drops_the_response_stream() {
    bounded(async {
        let mut client = Client::new();
        client
            .send(b"GET http://one.test/large HTTP/1.1\r\nHost: x\r\n\r\n")
            .await;
        let mut origin = client.origin("one.test", 80).await;
        head(&mut origin).await;
        origin
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
            .await
            .unwrap();
        client.status(200).await;
        let payload = vec![b'x'; 1024 * 1024];
        assert!(
            tokio::time::timeout(Duration::from_secs(1), origin.write_all(&payload))
                .await
                .is_err(),
            "slow client allowed an entire MiB to accumulate"
        );
        client.finish().await;
        assert_eq!(origin.read(&mut [0]).await.unwrap(), 0);
        let mut received = Vec::new();
        client.io.read_to_end(&mut received).await.unwrap();
        assert!(!received.is_empty());
        assert!(received.len() < payload.len());
        assert!(
            received.iter().all(|byte| *byte == b'x'),
            "failure injected HTTP text after response headers"
        );
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn a_stalled_destination_applies_backpressure_to_an_upload() {
    bounded(async {
        let mut client = Client::new();
        client.send(b"POST http://one.test/upload HTTP/1.1\r\nHost: x\r\nContent-Length: 1048576\r\n\r\n").await;
        let mut origin = client.origin("one.test", 80).await;
        head(&mut origin).await;
        let payload = vec![b'x'; 1024 * 1024];
        assert!(tokio::time::timeout(Duration::from_secs(1), client.io.write_all(&payload)).await.is_err(), "stalled destination allowed an entire upload to accumulate");
        client.finish().await;
        let mut received = Vec::new();
        origin.read_to_end(&mut received).await.unwrap();
        assert!(!received.is_empty());
        assert!(received.len() < payload.len());
        assert!(received.iter().all(|byte| *byte == b'x'));
    }).await;
}

#[tokio::test(start_paused = true)]
async fn cancellation_drops_pending_opens_without_acknowledging_or_replaying() {
    bounded(async {
        for method in ["GET http://one.test/", "CONNECT one.test:80"] {
            let mut client = Client::new();
            client
                .send(format!("{method} HTTP/1.1\r\nHost: x\r\n\r\n").as_bytes())
                .await;
            let opening = client.opened.recv().await.unwrap();
            client.finish().await;
            assert!(
                opening.reply.is_closed(),
                "pending open outlived its runner"
            );
            assert_eq!(client.io.read(&mut [0]).await.unwrap(), 0);
            assert!(client.opened.try_recv().is_err());
        }
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn dropping_runner_synchronously_closes_streamed_responses_and_quiet_tunnels() {
    bounded(async {
        for connect in [false, true] {
            let (opens, mut opened) = mpsc::unbounded_channel();
            let (_available, current) = watch::channel(Some(opens));
            let (client, stream) = tokio::io::duplex(1024);
            let mut client = BufReader::new(client);
            let mut runner = Box::pin(run(stream, Scripted(current)));
            let mut origin = tokio::select! {
                _ = &mut runner => panic!("runner exited during active stream"),
                origin = async {
                    let method = if connect { "CONNECT one.test:80" } else { "GET http://one.test/" };
                    client.write_all(format!("{method} HTTP/1.1\r\nHost: x\r\n\r\n").as_bytes()).await.unwrap();
                    let mut origin = opened.recv().await.unwrap().accept("one.test", 80);
                    if !connect {
                        head(&mut origin).await;
                        origin.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n").await.unwrap();
                    }
                    assert!(head(&mut client).await.starts_with("HTTP/1.1 200 "));
                    origin.write_all(b"prefix").await.unwrap();
                    let mut bytes = [0; 6];
                    client.read_exact(&mut bytes).await.unwrap();
                    assert_eq!(&bytes, b"prefix");
                    tokio::time::sleep(Duration::from_secs(60)).await;
                    origin.write_all(b"still-alive").await.unwrap();
                    let mut bytes = [0; 11];
                    client.read_exact(&mut bytes).await.unwrap();
                    assert_eq!(&bytes, b"still-alive");
                    origin
                } => origin,
            };
            drop(runner);
            assert!(matches!(futures_util::poll!(std::pin::pin!(origin.read(&mut [0]))), std::task::Poll::Ready(Ok(0))));
            assert!(matches!(futures_util::poll!(std::pin::pin!(client.read(&mut [0]))), std::task::Poll::Ready(Ok(0))));
        }
    }).await;
}

#[tokio::test(start_paused = true)]
async fn destination_eof_after_response_headers_closes_without_another_response_or_replay() {
    bounded(async {
        let mut client = Client::new();
        client
            .send(b"POST http://one.test/counted HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            .await;
        let mut origin = client.origin("one.test", 80).await;
        assert!(
            head(&mut origin)
                .await
                .starts_with("POST /counted HTTP/1.1\r\n")
        );
        origin
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nprefix")
            .await
            .unwrap();
        client.status(200).await;
        let mut prefix = [0; 6];
        client.io.read_exact(&mut prefix).await.unwrap();
        assert_eq!(&prefix, b"prefix");
        let (next, mut replacement) = mpsc::unbounded_channel();
        client.available.send_replace(Some(next));
        drop(origin);
        let mut rest = Vec::new();
        client.io.read_to_end(&mut rest).await.unwrap();
        assert!(
            rest.is_empty(),
            "injected a second response into a failed body"
        );
        assert!(replacement.try_recv().is_err(), "replayed a started POST");
        assert!(client.opened.try_recv().is_err());
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn destination_first_half_close_keeps_the_tunnel_and_runner_alive_for_client_bytes() {
    bounded(async {
        let mut client = Client::new();
        client
            .send(b"CONNECT one.test:80 HTTP/1.1\r\nHost: x\r\n\r\n")
            .await;
        let mut origin = client.origin("one.test", 80).await;
        client.status(200).await;
        origin.write_all(b"final-reply").await.unwrap();
        origin.shutdown().await.unwrap();
        let mut reply = Vec::new();
        client.io.read_to_end(&mut reply).await.unwrap();
        assert_eq!(reply, b"final-reply");
        assert!(
            !client.runner.is_finished(),
            "destination EOF ended the client connection"
        );
        client.send(b"client-after-eof").await;
        client.io.shutdown().await.unwrap();
        let mut request = Vec::new();
        origin.read_to_end(&mut request).await.unwrap();
        assert_eq!(request, b"client-after-eof");
        (&mut client.runner).await.unwrap();
    })
    .await;
}
