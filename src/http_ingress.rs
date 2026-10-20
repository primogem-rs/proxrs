//! Bounded HTTP/1 framing preflight. Hyper owns HTTP semantics and body decoding;
//! httparse validates heads/chunk sizes before Hyper can normalize ambiguity away.
use hyper::StatusCode;
use std::{
    io,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader},
    net::tcp::OwnedReadHalf,
    sync::mpsc,
};

pub const HEADER_LIMIT: usize = 32 * 1024;
pub const HEADER_COUNT: usize = HEADER_LIMIT / 4;
const HEADER_DEADLINE: Duration = Duration::from_secs(10);

pub async fn relay(
    socket: OwnedReadHalf,
    mut output: impl AsyncWrite + Unpin,
    heads: mpsc::Sender<Result<Arc<AtomicBool>, StatusCode>>,
) -> io::Result<()> {
    let mut input = BufReader::with_capacity(8192, socket);
    let mut first_head = true;
    loop {
        // An established response/tunnel has no idle timeout. Only a new
        // request's first byte starts later header deadlines; the initial
        // accepted connection still has a bounded wait for its first head.
        if !first_head && input.fill_buf().await?.is_empty() {
            return output.shutdown().await;
        }
        first_head = false;
        let parsed = match tokio::time::timeout(HEADER_DEADLINE, read_head(&mut input)).await {
            Ok(Ok(Some(head))) => match framing(&head) {
                Ok(body) => Ok((head, body)),
                Err(status) => Err(status),
            },
            Ok(Ok(None)) => return output.shutdown().await,
            Ok(Err(status)) => Err(status),
            Err(_) => Err(StatusCode::GATEWAY_TIMEOUT),
        };
        let malformed_body = Arc::new(AtomicBool::new(false));
        let (head, body) = match parsed {
            Ok(parsed) => {
                if heads.send(Ok(malformed_body.clone())).await.is_err() {
                    return Ok(());
                }
                parsed
            }
            Err(status) => {
                if heads.send(Err(status)).await.is_ok() {
                    // A valid, bodyless placeholder lets Hyper order this error
                    // after earlier responses on a pipelined connection. The
                    // status travels out of band, never in a client header.
                    output.write_all(b"GET http://invalid.local/ HTTP/1.1\r\nHost: invalid.local\r\nConnection: close\r\n\r\n").await?;
                }
                return output.shutdown().await;
            }
        };
        output.write_all(&head).await?;
        let copied = match body {
            Framing::Empty => Ok(()),
            Framing::Length(size) => copy_exact(&mut input, &mut output, size).await,
            Framing::Chunked => copy_chunks(&mut input, &mut output).await,
            Framing::Tunnel => {
                tokio::io::copy_buf(&mut input, &mut output).await?;
                return output.shutdown().await;
            }
        };
        if let Err(error) = copied {
            malformed_body.store(true, Ordering::Release);
            let _ = output.shutdown().await;
            return Err(error);
        }
    }
}

async fn read_head(input: &mut (impl AsyncBufRead + Unpin)) -> Result<Option<Vec<u8>>, StatusCode> {
    let mut head = Vec::with_capacity(1024);
    loop {
        let bytes = input
            .fill_buf()
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        if bytes.is_empty() {
            return if head.is_empty() {
                Ok(None)
            } else {
                Err(StatusCode::BAD_REQUEST)
            };
        }
        let mut consumed = 0;
        for &byte in bytes {
            if (byte == b'\n' && head.last() != Some(&b'\r'))
                || (head.last() == Some(&b'\r') && byte != b'\n')
            {
                return Err(StatusCode::BAD_REQUEST);
            }
            head.push(byte);
            consumed += 1;
            if head.ends_with(b"\r\n\r\n") {
                input.consume(consumed);
                return Ok(Some(head));
            }
            if head.len() >= HEADER_LIMIT {
                return Err(StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE);
            }
        }
        input.consume(consumed);
    }
}

enum Framing {
    Empty,
    Length(u64),
    Chunked,
    Tunnel,
}
fn framing(head: &[u8]) -> Result<Framing, StatusCode> {
    let bad = StatusCode::BAD_REQUEST;
    let mut fields = vec![httparse::EMPTY_HEADER; HEADER_COUNT];
    let mut request = httparse::Request::new(&mut fields);
    if !matches!(request.parse(head), Ok(httparse::Status::Complete(size)) if size == head.len())
        || request.version != Some(1)
    {
        return Err(bad);
    }
    let mut hosts = 0;
    let mut length = None;
    let mut transfer = false;
    for header in request.headers.iter() {
        if header.name.eq_ignore_ascii_case("host") {
            hosts += 1;
            if header.value.is_empty() {
                return Err(bad);
            }
        }
        if header.name.eq_ignore_ascii_case("content-length") {
            if length.is_some()
                || header.value.is_empty()
                || !header.value.iter().all(u8::is_ascii_digit)
            {
                return Err(bad);
            }
            length = Some(
                std::str::from_utf8(header.value)
                    .map_err(|_| bad)?
                    .parse::<u64>()
                    .map_err(|_| bad)?,
            );
        }
        if header.name.eq_ignore_ascii_case("transfer-encoding") {
            if transfer || !header.value.eq_ignore_ascii_case(b"chunked") {
                return Err(bad);
            }
            transfer = true;
        }
    }
    if hosts != 1 || (transfer && length.is_some()) {
        return Err(bad);
    }
    if request.method == Some("CONNECT") {
        if transfer || length.is_some_and(|size| size != 0) {
            return Err(bad);
        }
        return Ok(Framing::Tunnel);
    }
    Ok(if transfer {
        Framing::Chunked
    } else if let Some(size) = length {
        Framing::Length(size)
    } else {
        Framing::Empty
    })
}

async fn copy_exact(
    input: &mut (impl AsyncBufRead + Unpin),
    output: &mut (impl AsyncWrite + Unpin),
    size: u64,
) -> io::Result<()> {
    let copied = tokio::io::copy_buf(&mut input.take(size), output).await?;
    if copied != size {
        return Err(io::ErrorKind::UnexpectedEof.into());
    }
    Ok(())
}
async fn line(input: &mut (impl AsyncBufRead + Unpin)) -> io::Result<Vec<u8>> {
    let mut line = Vec::new();
    loop {
        let bytes = input.fill_buf().await?;
        if bytes.is_empty() {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        let take = bytes
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(bytes.len(), |pos| pos + 1);
        if line.len() + take > HEADER_LIMIT {
            return Err(io::ErrorKind::InvalidData.into());
        }
        line.extend_from_slice(&bytes[..take]);
        input.consume(take);
        if line.ends_with(b"\n") {
            return if line.ends_with(b"\r\n") {
                Ok(line)
            } else {
                Err(io::ErrorKind::InvalidData.into())
            };
        }
    }
}
async fn copy_chunks(
    input: &mut (impl AsyncBufRead + Unpin),
    output: &mut (impl AsyncWrite + Unpin),
) -> io::Result<()> {
    loop {
        let line = line(input).await?;
        let size = match httparse::parse_chunk_size(&line) {
            Ok(httparse::Status::Complete((end, size))) if end == line.len() => size,
            _ => return Err(io::ErrorKind::InvalidData.into()),
        };
        output.write_all(&line).await?;
        if size == 0 {
            let mut trailers = Vec::new();
            loop {
                let line = self::line(input).await?;
                if trailers.len() + line.len() > HEADER_LIMIT {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                trailers.extend_from_slice(&line);
                if line == b"\r\n" {
                    break;
                }
            }
            let mut fields = vec![httparse::EMPTY_HEADER; HEADER_COUNT];
            if !matches!(httparse::parse_headers(&trailers, &mut fields), Ok(httparse::Status::Complete((end, _))) if end == trailers.len())
            {
                return Err(io::ErrorKind::InvalidData.into());
            }
            output.write_all(&trailers).await?;
            return Ok(());
        }
        copy_exact(input, output, size).await?;
        let mut end = [0u8; 2];
        input.read_exact(&mut end).await?;
        if end != *b"\r\n" {
            return Err(io::ErrorKind::InvalidData.into());
        }
        output.write_all(&end).await?;
    }
}
