use crate::{cli::ConnectionOptions, terminal, trust};
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
    sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot, watch},
};
use tokio_util::sync::CancellationToken;

const DEADLINE: Duration = Duration::from_secs(10);
const CHANNELS: usize = 128;

#[derive(Debug, thiserror::Error)]
#[error("transient SSH connection failure")]
struct TransientFailure;

pub fn retryable(error: &anyhow::Error) -> bool {
    error.is::<TransientFailure>()
}

fn network_error(error: anyhow::Error) -> anyhow::Error {
    error.context(TransientFailure)
}

fn transport_error(error: anyhow::Error) -> anyhow::Error {
    let transient = error.downcast_ref::<russh::Error>().is_some_and(|error| {
        matches!(
            error,
            russh::Error::Disconnect
                | russh::Error::HUP
                | russh::Error::ConnectionTimeout
                | russh::Error::KeepaliveTimeout
                | russh::Error::Elapsed(_)
                | russh::Error::SendError
                | russh::Error::RecvError
                | russh::Error::IO(_)
        )
    });
    if transient {
        network_error(error)
    } else {
        error
    }
}

pub struct Connection {
    handle: Arc<client::Handle<TrustHandler>>,
    capacity: Arc<Semaphore>,
    cancel: CancellationToken,
    _socket: SocketGuard,
    lost: watch::Receiver<Option<ConnectionLoss>>,
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
    approvals: mpsc::Sender<HostApproval>,
    lost: watch::Sender<Option<ConnectionLoss>>,
}
#[derive(Clone, Copy)]
enum ConnectionLoss {
    Transport,
    Authentication,
    Policy,
    Protocol,
}

impl ConnectionLoss {
    fn error(self) -> anyhow::Error {
        match self {
            Self::Transport => network_error(anyhow::anyhow!("SSH transport connection lost")),
            Self::Authentication => anyhow::anyhow!(
                "SSH password authentication rejected or authentication methods exhausted"
            ),
            Self::Policy => anyhow::anyhow!("SSH server policy refused this connection"),
            Self::Protocol => anyhow::anyhow!(
                "SSH server ended the connection for a protocol or key-verification error"
            ),
        }
    }
}

impl Drop for TrustHandler {
    fn drop(&mut self) {
        // Driver completion is observable even when I/O shutdown fails before
        // its disconnect callback. No arbitrary server description is retained.
        if self.lost.borrow().is_none() {
            self.lost.send_replace(Some(ConnectionLoss::Transport));
        }
    }
}

async fn loss_reason(lost: &mut watch::Receiver<Option<ConnectionLoss>>) -> ConnectionLoss {
    loop {
        if let Some(reason) = *lost.borrow() {
            return reason;
        }
        if lost.changed().await.is_err() {
            return ConnectionLoss::Transport;
        }
    }
}

async fn auth_result(
    result: Result<client::AuthResult, russh::Error>,
    lost: &mut watch::Receiver<Option<ConnectionLoss>>,
    deadline: tokio::time::Instant,
) -> Result<client::AuthResult> {
    match result {
        Ok(result) => Ok(result),
        Err(
            russh::Error::SendError
            | russh::Error::RecvError
            | russh::Error::Disconnect
            | russh::Error::HUP,
        ) => {
            // A closed auth reply channel alone cannot distinguish an outage
            // from an explicit server authentication-exhaustion disconnect.
            let cause = tokio::time::timeout_at(deadline, loss_reason(lost))
                .await
                .context("SSH startup deadline exceeded (10 seconds)")
                .map_err(network_error)?;
            Err(cause.error())
        }
        Err(error) => Err(transport_error(error.into())),
    }
}

struct HostApproval {
    key: ssh_key::PublicKey,
    response: oneshot::Sender<()>,
}
impl client::Handler for TrustHandler {
    type Error = anyhow::Error;
    async fn disconnected(&mut self, reason: client::DisconnectReason<Self::Error>) -> Result<()> {
        let cause = match reason {
            client::DisconnectReason::ReceivedDisconnect(info) => match info.reason_code {
                russh::Disconnect::ConnectionLost
                | russh::Disconnect::TooManyConnections
                | russh::Disconnect::ServiceNotAvailable => ConnectionLoss::Transport,
                russh::Disconnect::AuthCancelledByUser
                | russh::Disconnect::NoMoreAuthMethodsAvailable
                | russh::Disconnect::IllegalUserName => ConnectionLoss::Authentication,
                russh::Disconnect::HostNotAllowedToConnect => ConnectionLoss::Policy,
                _ => ConnectionLoss::Protocol,
            },
            client::DisconnectReason::Error(error) => {
                if retryable(&transport_error(error)) {
                    ConnectionLoss::Transport
                } else {
                    ConnectionLoss::Protocol
                }
            }
        };
        self.lost.send_replace(Some(cause));
        Ok(())
    }

    async fn check_server_key(&mut self, key: &PublicKeyOrCertificate) -> Result<bool> {
        let PublicKeyOrCertificate::PublicKey { key, .. } = key else {
            bail!("SSH host certificates are unsupported");
        };
        if !trust::verify(&self.known_hosts, &self.host, self.port, &key.to_bytes()?)? {
            let (response, approved) = oneshot::channel();
            self.approvals
                .send(HostApproval {
                    key: key.clone(),
                    response,
                })
                .await
                .context("host approval is unavailable")?;
            approved.await.context("SSH host key was not approved")?;
        }
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
    pub async fn connect(
        options: &ConnectionOptions,
        password: &mut Option<zeroize::Zeroizing<String>>,
    ) -> Result<Self> {
        let mut deadline = tokio::time::Instant::now() + DEADLINE;
        let operation = async {
            let stream = tokio::time::timeout_at(
                deadline,
                TcpStream::connect((options.host.as_str(), options.ssh_port)),
            )
            .await
            .context("SSH startup deadline exceeded (10 seconds)")
            .map_err(network_error)?
            .context("could not connect to SSH server")
            .map_err(network_error)?
            .into_std()?;
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
            let (approval_sender, mut approvals) = mpsc::channel::<HostApproval>(1);
            let known_hosts = options.known_hosts_path()?;
            let (lost_sender, mut lost) = watch::channel(None);
            let handler = TrustHandler {
                host: options.host.clone(),
                port: options.ssh_port,
                known_hosts: known_hosts.clone(),
                approvals: approval_sender,
                lost: lost_sender,
            };
            let handshaking =
                client::connect_stream(Arc::new(config), TcpStream::from_std(stream)?, handler);
            tokio::pin!(handshaking);
            let mut handle = loop {
                tokio::select! {
                    result = &mut handshaking => break result.map_err(|error| {
                        match *lost.borrow() {
                            Some(cause @ (ConnectionLoss::Authentication | ConnectionLoss::Policy | ConnectionLoss::Protocol)) => cause.error(),
                            _ => transport_error(explain_handshake_error(error)),
                        }
                    }).context("SSH handshake or host verification failed")?,
                    Some(approval) = approvals.recv() => {
                        // Input lives in this task, not the SSH driver's callback:
                        // cancelling startup immediately drops the terminal guard.
                        let started = tokio::time::Instant::now();
                        terminal::approve_host(&options.host, options.ssh_port,
                            approval.key.algorithm().as_str(),
                            &approval.key.fingerprint(ssh_key::HashAlg::Sha256).to_string()).await?;
                        deadline += started.elapsed();
                        trust::save(&known_hosts, &options.host, options.ssh_port, &approval.key)?;
                        let _ = approval.response.send(());
                    }
                    _ = tokio::time::sleep_until(deadline) => return Err(network_error(anyhow::anyhow!("SSH startup deadline exceeded (10 seconds)"))),
                }
            };
            if let russh::client::AuthResult::Failure {
                remaining_methods, ..
            } = auth_result(
                tokio::time::timeout_at(deadline, handle.authenticate_none(&options.user))
                    .await
                    .context("SSH startup deadline exceeded (10 seconds)")
                    .map_err(network_error)?,
                &mut lost,
                deadline,
            )
            .await
            .context("could not query SSH authentication methods")?
                && !remaining_methods.contains(&russh::MethodKind::Password)
            {
                bail!(
                    "SSH server requires unsupported authentication; password authentication must be enabled (keyboard-interactive/MFA is unsupported)"
                );
            }
            if password.is_none() {
                let started = tokio::time::Instant::now();
                *password = Some(terminal::password_from_terminal().await?);
                deadline += started.elapsed();
            }
            match auth_result(
                tokio::time::timeout_at(
                    deadline,
                    handle.authenticate_password(
                        &options.user,
                        password.as_ref().expect("password obtained").as_str(),
                    ),
                )
                .await
                .context("SSH startup deadline exceeded (10 seconds)")
                .map_err(network_error)?,
                &mut lost,
                deadline,
            )
            .await
            .context("SSH password authentication failed")?
            {
                russh::client::AuthResult::Success => {}
                russh::client::AuthResult::Failure {
                    partial_success: true,
                    ..
                } => bail!("SSH authentication requires an unsupported additional method"),
                russh::client::AuthResult::Failure { .. } => {
                    bail!("SSH password authentication rejected")
                }
            }
            Ok(Self {
                handle: Arc::new(handle),
                capacity: Arc::new(Semaphore::new(CHANNELS)),
                cancel: CancellationToken::new(),
                _socket: socket,
                lost,
            })
        };
        operation.await
    }

    pub async fn lost(&self) -> anyhow::Error {
        loss_reason(&mut self.lost.clone()).await.error()
    }

    pub fn close(&self) {
        self.cancel.cancel();
        let _ = self._socket.0.shutdown(Shutdown::Both);
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

fn explain_handshake_error(error: anyhow::Error) -> anyhow::Error {
    if let Some(russh::Error::NoCommonAlgo {
        kind: russh::AlgorithmKind::Key,
        theirs,
        ..
    }) = error.downcast_ref::<russh::Error>()
        && !theirs.is_empty()
        && theirs
            .iter()
            .all(|name| name.ends_with("-cert-v01@openssh.com"))
    {
        return anyhow::anyhow!(
            "SSH host certificates are unsupported; the server must offer an ordinary host key"
        );
    }
    error
}
