use crate::http_ingress;
use crate::transport::{Connection, DestinationError};
use bytes::Bytes;
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
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
};
use tokio::{
    io::AsyncWriteExt,
    net::{TcpListener, TcpStream},
    sync::{Mutex, OwnedSemaphorePermit, Semaphore, mpsc, watch},
    task::JoinSet,
};
use tokio_util::sync::CancellationToken;

pub async fn serve(
    listener: TcpListener,
    ssh: watch::Receiver<Option<Arc<Connection>>>,
    cancel: CancellationToken,
) -> anyhow::Result<()> {
    let capacity = Arc::new(Semaphore::new(256));
    let mut clients = JoinSet::new();
    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            Some(_) = clients.join_next(), if !clients.is_empty() => {},
            accepted = listener.accept() => {
                let (stream, _) = accepted?;
                let Ok(permit) = capacity.clone().try_acquire_owned() else { drop(stream); continue; };
                let ssh = ssh.clone();
                let cancel = cancel.clone();
                clients.spawn(serve_client(stream, ssh, cancel, permit));
            }
        }
    }
    clients.abort_all();
    while clients.join_next().await.is_some() {}
    Ok(())
}

async fn serve_client(
    stream: TcpStream,
    ssh: watch::Receiver<Option<Arc<Connection>>>,
    cancel: CancellationToken,
    permit: OwnedSemaphorePermit,
) {
    let permit = Arc::new(permit);
    let cancel = cancel.child_token();
    let _cancel_on_drop = cancel.clone().drop_guard();
    let (input, mut output) = stream.into_split();
    let (http_io, bridge) = tokio::io::duplex(16 * 1024);
    let (mut responses, requests) = tokio::io::split(bridge);
    let (head_sender, receiver) = mpsc::channel(8);
    let heads = Arc::new(Mutex::new(receiver));
    let mut workers = JoinSet::new();
    workers.spawn(async move {
        let _ = http_ingress::relay(input, requests, head_sender).await;
    });
    let service_cancel = cancel.clone();
    let service = service_fn(move |request| {
        let heads = heads.clone();
        let ssh = ssh.clone();
        let cancel = service_cancel.clone();
        let permit = permit.clone();
        async move {
            match heads.lock().await.recv().await {
                Some(Ok(malformed)) => {
                    let current = ssh.borrow().clone();
                    match current {
                        Some(connection) => {
                            handle(request, connection, cancel, permit, malformed).await
                        }
                        None => Ok(response(StatusCode::SERVICE_UNAVAILABLE)),
                    }
                }
                Some(Err(status)) => Ok(response(status)),
                None => Ok(response(StatusCode::BAD_REQUEST)),
            }
        }
    });
    workers.spawn(async move {
        let _ = http1::Builder::new()
            .max_buf_size(http_ingress::HEADER_LIMIT)
            .max_header_size(http_ingress::HEADER_LIMIT)
            .max_headers(http_ingress::HEADER_COUNT)
            .half_close(true)
            .serve_connection(TokioIo::new(http_io), service)
            .with_upgrades()
            .await;
    });
    tokio::select! {
        _ = cancel.cancelled() => {},
        _ = tokio::io::copy(&mut responses, &mut output) => {},
    }
    let _ = output.shutdown().await;
    cancel.cancel();
    workers.abort_all();
    while workers.join_next().await.is_some() {}
}

async fn handle(
    mut request: Request<Incoming>,
    ssh: Arc<Connection>,
    cancel: CancellationToken,
    permit: Arc<OwnedSemaphorePermit>,
    malformed: Arc<AtomicBool>,
) -> Result<Response<ProxyBody>, Infallible> {
    if request.method() != Method::CONNECT {
        return Ok(forward(request, ssh, cancel, malformed).await);
    }
    let Some(authority) = request.uri().authority() else {
        return Ok(response(StatusCode::BAD_REQUEST));
    };
    if request.uri().scheme().is_some()
        || request.uri().path_and_query().is_some()
        || authority.as_str().contains('@')
    {
        return Ok(response(StatusCode::BAD_REQUEST));
    }
    let host = authority
        .host()
        .trim_start_matches('[')
        .trim_end_matches(']');
    let Some(port) = authority.port_u16().filter(|port| *port != 0) else {
        return Ok(response(StatusCode::BAD_REQUEST));
    };
    if host.is_empty()
        || ((host.contains(':') || authority.as_str().starts_with('['))
            && (!authority.as_str().starts_with('[')
                || host.parse::<std::net::Ipv6Addr>().is_err()))
    {
        return Ok(response(StatusCode::BAD_REQUEST));
    }
    let mut channel = match ssh.open(host.to_owned(), port).await {
        Ok(channel) => channel,
        Err(error) => {
            return Ok(destination_error(error));
        }
    };
    let upgrade = hyper::upgrade::on(&mut request);
    tokio::spawn(async move {
        let _permit = permit;
        tokio::select! {
            _ = cancel.cancelled() => {},
            _ = async {
                if let Ok(upgraded) = upgrade.await {
                    let mut client = TokioIo::new(upgraded);
                    let _ = tokio::io::copy_bidirectional(&mut client, &mut channel).await;
                }
            } => {},
        }
    });
    Ok(response(StatusCode::OK))
}
fn destination_error(error: DestinationError) -> Response<ProxyBody> {
    eprintln!("{error}");
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
            driver: None,
        })
        .expect("static response is valid")
}

type BodyError = Box<dyn std::error::Error + Send + Sync>;
struct ProxyBody {
    inner: UnsyncBoxBody<Bytes, BodyError>,
    driver: Option<tokio::task::JoinHandle<()>>,
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
impl Drop for ProxyBody {
    fn drop(&mut self) {
        if let Some(driver) = &self.driver {
            driver.abort();
        }
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

async fn forward(
    mut request: Request<Incoming>,
    ssh: Arc<Connection>,
    cancel: CancellationToken,
    malformed: Arc<AtomicBool>,
) -> Response<ProxyBody> {
    if request.uri().scheme_str() != Some("http") {
        return response(StatusCode::BAD_REQUEST);
    }
    let Some(authority) = request.uri().authority().cloned() else {
        return response(StatusCode::BAD_REQUEST);
    };
    let host = authority
        .host()
        .trim_start_matches('[')
        .trim_end_matches(']');
    let port = authority.port_u16().unwrap_or(80);
    if host.is_empty()
        || authority.as_str().contains('@')
        || port == 0
        || (authority.port().is_some() && authority.port_u16().is_none())
        || ((host.contains(':') || authority.as_str().starts_with('['))
            && (!authority.as_str().starts_with('[')
                || host.parse::<std::net::Ipv6Addr>().is_err()))
    {
        return response(StatusCode::BAD_REQUEST);
    }
    let channel = match ssh.open(host.to_owned(), port).await {
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
    let driver = tokio::spawn(async move {
        tokio::select! { _ = cancel.cancelled() => {}, _ = connection => {} }
    });
    match sender.send_request(request).await {
        Ok(mut upstream) => {
            strip_hop_headers(upstream.headers_mut());
            upstream.map(|body| ProxyBody {
                inner: body
                    .map_err(|error| -> BodyError { Box::new(error) })
                    .boxed_unsync(),
                driver: Some(driver),
            })
        }
        Err(_) => {
            driver.abort();
            response(if malformed.load(Ordering::Acquire) {
                StatusCode::BAD_REQUEST
            } else {
                StatusCode::BAD_GATEWAY
            })
        }
    }
}
