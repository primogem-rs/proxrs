use crate::transport::{Connection, DestinationError};
use bytes::Bytes;
use http_body_util::Full;
use hyper::{
    Method, Request, Response, StatusCode, body::Incoming, server::conn::http1, service::service_fn,
};
use hyper_util::rt::{TokioIo, TokioTimer};
use std::{convert::Infallible, sync::Arc, time::Duration};
use tokio::{
    net::TcpListener,
    sync::{OwnedSemaphorePermit, Semaphore},
    task::JoinSet,
};
use tokio_util::sync::CancellationToken;

pub async fn serve(
    listener: TcpListener,
    ssh: Arc<Connection>,
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
                clients.spawn(async move {
                    let permit = Arc::new(permit);
                    let service = service_fn(move |request| handle(request, ssh.clone(), cancel.clone(), permit.clone()));
                    let _ = http1::Builder::new().max_buf_size(32 * 1024).max_header_size(32 * 1024)
                        .timer(TokioTimer::new()).header_read_timeout(Duration::from_secs(10)).half_close(true)
                        .serve_connection(TokioIo::new(stream), service).with_upgrades().await;
                });
            }
        }
    }
    clients.abort_all();
    while clients.join_next().await.is_some() {}
    Ok(())
}

async fn handle(
    mut request: Request<Incoming>,
    ssh: Arc<Connection>,
    cancel: CancellationToken,
    permit: Arc<OwnedSemaphorePermit>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    if request.method() != Method::CONNECT {
        return Ok(response(StatusCode::BAD_REQUEST));
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
    if host.is_empty() || (host.contains(':') && host.parse::<std::net::Ipv6Addr>().is_err()) {
        return Ok(response(StatusCode::BAD_REQUEST));
    }
    let mut channel = match ssh.open(host.to_owned(), port).await {
        Ok(channel) => channel,
        Err(error) => {
            eprintln!("{error}");
            let status = match error {
                DestinationError::Unavailable | DestinationError::Capacity => {
                    StatusCode::SERVICE_UNAVAILABLE
                }
                DestinationError::Timeout => StatusCode::GATEWAY_TIMEOUT,
                DestinationError::Refused => StatusCode::BAD_GATEWAY,
            };
            return Ok(response(status));
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
fn response(status: StatusCode) -> Response<Full<Bytes>> {
    Response::builder()
        .status(status)
        .body(Full::new(Bytes::new()))
        .expect("static response is valid")
}
