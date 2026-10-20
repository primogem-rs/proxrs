use crate::{cli::ConnectionOptions, trust};
use anyhow::{Context, Result, bail};
use russh::{Channel, ChannelMsg, client, keys::PublicKeyOrCertificate};
use std::{
    io,
    net::Shutdown,
    pin::Pin,
    sync::Arc,
    task::{Context as TaskContext, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite, AsyncWriteExt, DuplexStream, ReadBuf},
    net::TcpStream,
    sync::{OwnedSemaphorePermit, Semaphore, oneshot},
};
use tokio_util::sync::CancellationToken;

const DEADLINE: Duration = Duration::from_secs(10);
const CHANNELS: usize = 128;

pub struct Connection {
    handle: Arc<client::Handle<TrustHandler>>,
    capacity: Arc<Semaphore>,
    cancel: CancellationToken,
    _socket: SocketGuard,
}
struct SocketGuard(std::net::TcpStream);
impl Drop for SocketGuard {
    fn drop(&mut self) {
        let _ = self.0.shutdown(Shutdown::Both);
    }
}
struct TrustHandler {
    host: String,
    port: u16,
    known_hosts: std::path::PathBuf,
}
impl client::Handler for TrustHandler {
    type Error = anyhow::Error;
    async fn check_server_key(&mut self, key: &PublicKeyOrCertificate) -> Result<bool> {
        let PublicKeyOrCertificate::PublicKey { key, .. } = key else {
            bail!("SSH host certificates are unsupported");
        };
        trust::verify(&self.known_hosts, &self.host, self.port, &key.to_bytes()?)?;
        Ok(true)
    }
}
#[derive(Debug, thiserror::Error)]
pub enum DestinationError {
    #[error("SSH connection unavailable")]
    Unavailable,
    #[error("destination channel capacity exhausted")]
    Capacity,
    #[error("destination connection deadline exceeded")]
    Timeout,
    #[error(
        "destination connection failed (refused or unreachable); check destination host and port"
    )]
    Refused,
    #[error("SSH forwarding denied; check AllowTcpForwarding and PermitOpen")]
    ForwardingDenied,
    #[error(
        "SSH server rejected the destination channel; check server channel support and resource limits"
    )]
    ChannelRejected,
}

pub struct DestinationStream {
    io: DuplexStream,
    cancel: CancellationToken,
}
impl AsyncRead for DestinationStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_read(cx, buffer)
    }
}
impl AsyncWrite for DestinationStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.io).poll_write(cx, bytes)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_shutdown(cx)
    }
}
impl Drop for DestinationStream {
    fn drop(&mut self) {
        self.cancel.cancel();
    }
}
impl Drop for Connection {
    fn drop(&mut self) {
        self.cancel.cancel();
    }
}

impl Connection {
    pub async fn connect(options: &ConnectionOptions, password: &str) -> Result<Self> {
        tokio::time::timeout(DEADLINE, async {
            let stream = TcpStream::connect((options.host.as_str(), options.ssh_port)).await.context("could not connect to SSH server")?.into_std()?;
            let socket = SocketGuard(stream.try_clone()?);
            let config = client::Config {
                window_size: 64 * 1024,
                maximum_packet_size: 16 * 1024,
                channel_buffer_size: 16,
                // Enabled by our narrowly scoped, documented upstream patch.
                direct_tcpip_receive_window_backpressure: true,
                keepalive_interval: Some(Duration::from_secs(15)),
                keepalive_max: 3,
                nodelay: true,
                ..Default::default()
            };
            let handler = TrustHandler { host: options.host.clone(), port: options.ssh_port, known_hosts: options.known_hosts_path()? };
            let mut handle = client::connect_stream(Arc::new(config), TcpStream::from_std(stream)?, handler).await.context("SSH handshake or host verification failed")?;
            if let russh::client::AuthResult::Failure { remaining_methods, .. } = handle.authenticate_none(&options.user).await.context("could not query SSH authentication methods")?
                && !remaining_methods.contains(&russh::MethodKind::Password) {
                bail!("SSH server requires unsupported authentication; password authentication must be enabled (keyboard-interactive/MFA is unsupported)");
            }
            match handle.authenticate_password(&options.user, password).await.context("SSH password authentication failed")? {
                russh::client::AuthResult::Success => {},
                russh::client::AuthResult::Failure { partial_success: true, .. } => bail!("SSH authentication requires an unsupported additional method"),
                russh::client::AuthResult::Failure { .. } => bail!("SSH password authentication rejected"),
            }
            Ok(Self { handle: Arc::new(handle), capacity: Arc::new(Semaphore::new(CHANNELS)), cancel: CancellationToken::new(), _socket: socket })
        }).await.context("SSH startup deadline exceeded (10 seconds)")?
    }

    pub fn is_closed(&self) -> bool {
        self.handle.is_closed()
    }

    pub async fn open(
        &self,
        host: String,
        port: u16,
    ) -> Result<DestinationStream, DestinationError> {
        if self.is_closed() || self.cancel.is_cancelled() {
            return Err(DestinationError::Unavailable);
        }
        let permit = self
            .capacity
            .clone()
            .try_acquire_owned()
            .map_err(|_| DestinationError::Capacity)?;
        let (sender, receiver) = oneshot::channel();
        let handle = self.handle.clone();
        let session_cancel = self.cancel.clone();
        // A timed-out caller does not release the pending channel's capacity.
        // Finish that channel independently and close a late success. Other
        // channel opens remain concurrent and cannot inherit its destination.
        tokio::spawn(async move {
            let opened = tokio::select! {
                _ = session_cancel.cancelled() => return,
                result = handle.channel_open_direct_tcpip(host, u32::from(port), "127.0.0.1", 0) => result,
            };
            let result = match opened {
                Ok(channel) => {
                    let (client, pump) = tokio::io::duplex(16 * 1024);
                    let stream_cancel = session_cancel.child_token();
                    tokio::spawn(relay(
                        channel,
                        pump,
                        stream_cancel.clone(),
                        session_cancel,
                        permit,
                    ));
                    Ok(DestinationStream {
                        io: client,
                        cancel: stream_cancel,
                    })
                }
                Err(_) if handle.is_closed() => Err(DestinationError::Unavailable),
                Err(russh::Error::ChannelOpenFailure(
                    russh::ChannelOpenFailure::AdministrativelyProhibited,
                )) => Err(DestinationError::ForwardingDenied),
                Err(russh::Error::ChannelOpenFailure(russh::ChannelOpenFailure::ConnectFailed)) => {
                    Err(DestinationError::Refused)
                }
                Err(_) => Err(DestinationError::ChannelRejected),
            };
            let _ = sender.send(result);
        });
        tokio::time::timeout(DEADLINE, receiver)
            .await
            .map_err(|_| DestinationError::Timeout)?
            .map_err(|_| DestinationError::Unavailable)?
    }
}

async fn relay(
    channel: Channel<client::Msg>,
    io: DuplexStream,
    cancel: CancellationToken,
    session_cancel: CancellationToken,
    _permit: OwnedSemaphorePermit,
) {
    let (mut receive, send) = channel.split();
    let (mut reader, mut writer) = tokio::io::split(io);
    {
        let mut remote_reader = receive.make_reader();
        let mut remote_writer = send.make_writer();
        let inbound = async {
            tokio::io::copy(&mut remote_reader, &mut writer).await?;
            writer.shutdown().await
        };
        let outbound = async {
            tokio::io::copy(&mut reader, &mut remote_writer).await?;
            remote_writer.shutdown().await
        };
        tokio::select! {
            _ = cancel.cancelled() => {},
            _ = async { tokio::try_join!(inbound, outbound) } => {},
        }
    }
    // Keep the permit until the channel has actually closed. No detached drop
    // tasks or abandoned opens can build an unbounded cleanup queue.
    tokio::select! {
        _ = session_cancel.cancelled() => {},
        _ = async {
            let _ = send.close().await;
            while let Some(message) = receive.wait().await {
                if matches!(message, ChannelMsg::Close) { break; }
            }
        } => {},
    }
}
