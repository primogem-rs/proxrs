//! One application's connection, including its HTTP bodies and any CONNECT tunnel.
//! All local work is polled here: dropping the runner drops its streams and futures.
mod ingress;
#[cfg(test)]
mod tests;

use crate::{
    terminal,
    transport::{Connection, DestinationError, DestinationStream},
};
use bytes::Bytes;
use futures_util::{FutureExt, StreamExt, stream::FuturesUnordered};
use http_body_util::{BodyExt, Full, combinators::UnsyncBoxBody};
use hyper::{
    Method, Request, Response, StatusCode,
    body::{Body, Frame, Incoming, SizeHint},
    server::conn::http1,
    service::service_fn,
};
use hyper_util::rt::TokioIo;
use std::{
    convert::Infallible,
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
};
use tokio::{
    io::{AsyncRead, AsyncWrite, AsyncWriteExt},
    net::TcpStream,
    sync::{Mutex, mpsc, watch},
};
use tokio_util::sync::CancellationToken;

// Selection precedes destination validation. Consuming that selection on open
// pins a request to one SSH connection, even if supervision replaces it meanwhile.
trait Destinations: Clone + Send + 'static {
    type Selected: SelectedDestination;
    fn select(&self) -> Option<Self::Selected>;
}
trait SelectedDestination: Send {
    type Stream: AsyncRead + AsyncWrite + Unpin + Send + 'static;
    fn open(
        self,
        host: String,
        port: u16,
    ) -> impl Future<Output = Result<Self::Stream, DestinationError>> + Send;
}

#[derive(Clone)]
struct SshDestinations(watch::Receiver<Option<Arc<Connection>>>);
impl Destinations for SshDestinations {
    type Selected = Arc<Connection>;
    fn select(&self) -> Option<Self::Selected> {
        self.0.borrow().clone()
    }
}
impl SelectedDestination for Arc<Connection> {
    type Stream = DestinationStream;
    async fn open(self, host: String, port: u16) -> Result<Self::Stream, DestinationError> {
        Connection::open(&self, host, port).await
    }
}

pub async fn serve(stream: TcpStream, ssh: watch::Receiver<Option<Arc<Connection>>>) {
    run(stream, SshDestinations(ssh)).await;
}

type Child = Pin<Box<dyn Future<Output = ()> + Send>>;

async fn run<I, D>(stream: I, destinations: D)
where
    I: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    D: Destinations,
{
    let (input, mut output) = tokio::io::split(stream);
    let (http_io, bridge) = tokio::io::duplex(16 * 1024);
    let (mut responses, requests) = tokio::io::split(bridge);
    let (head_sender, receiver) = mpsc::channel(8);
    let heads = Arc::new(Mutex::new(receiver));
    let ingress = ingress::relay(input, requests, head_sender).fuse();
    // One sequential HTTP/1 service submits drivers/tunnels. Bound both queued
    // and running futures, including cancelled drivers awaiting their final poll.
    let (child_sender, mut child_receiver) = mpsc::channel::<Child>(1);
    let mut children = FuturesUnordered::new();
    let service = service_fn(move |request| {
        let heads = heads.clone();
        let destinations = destinations.clone();
        let children = child_sender.clone();
        async move {
            let result = match heads.lock().await.recv().await {
                Some(Ok(malformed)) => match destinations.select() {
                    Some(selected) => handle(request, selected, children, malformed).await,
                    None => response(StatusCode::SERVICE_UNAVAILABLE),
                },
                Some(Err(status)) => response(status),
                None => response(StatusCode::BAD_REQUEST),
            };
            Ok::<_, Infallible>(result)
        }
    });
    let http = http1::Builder::new()
        .max_buf_size(ingress::HEADER_LIMIT)
        .max_header_size(ingress::HEADER_LIMIT)
        .max_headers(ingress::HEADER_COUNT)
        .half_close(true)
        .serve_connection(TokioIo::new(http_io), service)
        .with_upgrades()
        .fuse();
    let outgoing = async {
        tokio::io::copy(&mut responses, &mut output).await?;
        output.shutdown().await
    }
    .fuse();
    tokio::pin!(ingress, http, outgoing);
    // Fuse drops completed drivers immediately, including Hyper's retained IO
    // after a body error, while the outgoing pump drains already-written bytes.
    let mut children_closed = false;
    let mut outgoing_done = false;
    loop {
        // Destination EOF half-closes CONNECT output; its child still owns the
        // client-to-destination direction. Keep the runner (and admission slot)
        // alive until that direction also finishes.
        if outgoing_done && children_closed && children.is_empty() {
            break;
        }
        tokio::select! {
            _ = &mut ingress => {},
            _ = &mut http => {},
            child = child_receiver.recv(), if !children_closed && children.len() < 2 => {
                match child {
                    Some(child) => children.push(child),
                    None => children_closed = true,
                }
            },
            Some(()) = children.next(), if !children.is_empty() => {},
            result = &mut outgoing => {
                if result.is_err() { break; }
                outgoing_done = true;
            },
        }
    }
}

async fn handle<D: SelectedDestination>(
    mut request: Request<Incoming>,
    destination: D,
    children: mpsc::Sender<Child>,
    malformed: Arc<AtomicBool>,
) -> Response<ProxyBody> {
    if request.method() != Method::CONNECT {
        return forward(request, destination, children, malformed).await;
    }
    let Some(authority) = request.uri().authority() else {
        return response(StatusCode::BAD_REQUEST);
    };
    if request.uri().scheme().is_some() || request.uri().path_and_query().is_some() {
        return response(StatusCode::BAD_REQUEST);
    }
    let Some((host, port)) = destination_authority(authority, None) else {
        return response(StatusCode::BAD_REQUEST);
    };
    let mut channel = match destination.open(host.to_owned(), port).await {
        Ok(channel) => channel,
        Err(error) => return destination_error(error),
    };
    let upgrade = hyper::upgrade::on(&mut request);
    let _ = children
        .send(Box::pin(async move {
            if let Ok(upgraded) = upgrade.await {
                let mut client = TokioIo::new(upgraded);
                let _ = tokio::io::copy_bidirectional(&mut client, &mut channel).await;
            }
        }))
        .await;
    response(StatusCode::OK)
}
// The HTTP library parses general URI authorities. A proxy destination has a
// stricter shape: an optional bracketed IPv6 literal and an actual TCP port.
// Inspect the complete text because Authority::host() discards text after ']'
// and Authority::port() treats invalid explicit ports as absent.
fn destination_authority(
    authority: &hyper::http::uri::Authority,
    default_port: Option<u16>,
) -> Option<(&str, u16)> {
    let text = authority.as_str();
    if text.contains('@') {
        return None;
    }
    let (host, port) = if let Some(bracketed) = text.strip_prefix('[') {
        let (host, suffix) = bracketed.split_once(']')?;
        host.parse::<std::net::Ipv6Addr>().ok()?;
        let port = if suffix.is_empty() {
            None
        } else {
            Some(suffix.strip_prefix(':')?)
        };
        (host, port)
    } else {
        if text.contains(['[', ']']) {
            return None;
        }
        text.split_once(':')
            .map_or((text, None), |(host, port)| (host, Some(port)))
    };
    if host.is_empty() {
        return None;
    }
    let port = match port {
        Some(port) if !port.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()) => {
            port.parse::<u16>().ok().filter(|port| *port != 0)?
        }
        Some(_) => return None,
        None => default_port?,
    };
    Some((host, port))
}

fn destination_error(error: DestinationError) -> Response<ProxyBody> {
    terminal::destination_failure(&error);
    response(match error {
        DestinationError::Unavailable | DestinationError::Capacity => {
            StatusCode::SERVICE_UNAVAILABLE
        }
        DestinationError::Timeout => StatusCode::GATEWAY_TIMEOUT,
        DestinationError::Refused
        | DestinationError::ForwardingDenied
        | DestinationError::ChannelRejected => StatusCode::BAD_GATEWAY,
    })
}

fn response(status: StatusCode) -> Response<ProxyBody> {
    let mut builder = Response::builder().status(status);
    if !status.is_success() {
        builder = builder.header(hyper::header::CONNECTION, "close");
    }
    builder
        .body(ProxyBody {
            inner: Full::new(Bytes::new())
                .map_err(|never| match never {})
                .boxed_unsync(),
            _driver: None,
        })
        .expect("static response is valid")
}

type BodyError = Box<dyn std::error::Error + Send + Sync>;
struct ProxyBody {
    inner: UnsyncBoxBody<Bytes, BodyError>,
    _driver: Option<tokio_util::sync::DropGuard>,
}
impl Body for ProxyBody {
    type Data = Bytes;
    type Error = BodyError;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BodyError>>> {
        Pin::new(&mut self.inner).poll_frame(cx)
    }
    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }
    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

fn strip_hop_headers(headers: &mut hyper::HeaderMap) {
    let named: Vec<_> = headers
        .get_all(hyper::header::CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|name| hyper::header::HeaderName::from_bytes(name.trim().as_bytes()).ok())
        .collect();
    for name in named {
        headers.remove(name);
    }
    for name in [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    ] {
        headers.remove(name);
    }
}

async fn forward<D: SelectedDestination>(
    mut request: Request<Incoming>,
    destination: D,
    children: mpsc::Sender<Child>,
    malformed: Arc<AtomicBool>,
) -> Response<ProxyBody> {
    if request.uri().scheme_str() != Some("http") {
        return response(StatusCode::BAD_REQUEST);
    }
    let Some(authority) = request.uri().authority().cloned() else {
        return response(StatusCode::BAD_REQUEST);
    };
    let Some((host, port)) = destination_authority(&authority, Some(80)) else {
        return response(StatusCode::BAD_REQUEST);
    };
    let channel = match destination.open(host.to_owned(), port).await {
        Ok(channel) => channel,
        Err(error) => {
            return destination_error(error);
        }
    };
    strip_hop_headers(request.headers_mut());
    request.headers_mut().insert(
        hyper::header::HOST,
        authority
            .as_str()
            .parse()
            .expect("URI authority is a header value"),
    );
    let path = request
        .uri()
        .path_and_query()
        .map_or("/", |value| value.as_str());
    *request.uri_mut() = path.parse().expect("parsed URI path remains valid");
    let Ok((mut sender, connection)) =
        hyper::client::conn::http1::handshake(TokioIo::new(channel)).await
    else {
        return response(StatusCode::BAD_GATEWAY);
    };
    let cancel = CancellationToken::new();
    let driver = cancel.clone().drop_guard();
    let _ = children
        .send(Box::pin(async move {
            tokio::select! { _ = cancel.cancelled() => {}, _ = connection => {} }
        }))
        .await;
    match sender.send_request(request).await {
        Ok(mut upstream) => {
            strip_hop_headers(upstream.headers_mut());
            upstream.map(|body| ProxyBody {
                inner: body
                    .map_err(|error| -> BodyError { Box::new(error) })
                    .boxed_unsync(),
                _driver: Some(driver),
            })
        }
        Err(_) => response(if malformed.load(Ordering::Acquire) {
            StatusCode::BAD_REQUEST
        } else {
            StatusCode::BAD_GATEWAY
        }),
    }
}
