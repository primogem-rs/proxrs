use crate::{
    cli::{ConnectionOptions, RunOptions},
    managed::ManagedCommand,
    proxy, terminal,
    transport::{self, Connection},
};
use anyhow::{Context, Result, anyhow};
use std::{sync::Arc, time::Duration};
use tokio::{
    net::TcpListener,
    signal::unix::{SignalKind, signal},
    sync::watch,
    time::Instant,
};
use tokio_util::sync::CancellationToken;
use zeroize::Zeroizing;

pub async fn up(options: ConnectionOptions) -> Result<i32> {
    supervise(&options, None).await
}

pub async fn managed(options: RunOptions) -> Result<i32> {
    supervise(&options.connection, Some(&options)).await
}

struct SshSession {
    connection: Arc<Connection>,
    password: Option<Zeroizing<String>>,
    retries: u32,
    ready_since: Instant,
}

impl SshSession {
    async fn start(options: &ConnectionOptions) -> Result<Self> {
        let mut password = if options.password_stdin {
            Some(terminal::password_from_stdin().await?)
        } else {
            None
        };
        let mut retries = 0;
        let connection = match Connection::connect(options, &mut password).await {
            Ok(connection) => Arc::new(connection),
            Err(error) if transport::retryable(&error) => {
                retry(options, &mut password, &mut retries).await?
            }
            Err(error) => return Err(error),
        };
        Ok(Self {
            connection,
            password,
            retries,
            ready_since: Instant::now(),
        })
    }

    async fn supervise(
        &mut self,
        options: &ConnectionOptions,
        available: &watch::Sender<Option<Arc<Connection>>>,
    ) -> Result<i32> {
        loop {
            let failure = self.connection.lost().await;
            if self.ready_since.elapsed() >= Duration::from_secs(60) {
                self.retries = 0;
            }
            available.send_replace(None);
            self.connection.close();
            terminal::ssh_connection_lost();
            if !transport::retryable(&failure) {
                return Err(failure);
            }
            self.connection = retry(options, &mut self.password, &mut self.retries).await?;
            self.ready_since = Instant::now();
            available.send_replace(Some(self.connection.clone()));
            terminal::ssh_connection_restored();
        }
    }
}

async fn retry(
    options: &ConnectionOptions,
    password: &mut Option<Zeroizing<String>>,
    retries: &mut u32,
) -> Result<Arc<Connection>> {
    while *retries < 3 {
        let delay = 1 << *retries;
        *retries += 1;
        terminal::ssh_retry(*retries, delay);
        tokio::time::sleep(Duration::from_secs(delay)).await;
        match Connection::connect(options, password).await {
            Ok(connection) => return Ok(Arc::new(connection)),
            Err(error) if transport::retryable(&error) => {
                if *retries == 3 {
                    return Err(error).context("SSH retry budget exhausted");
                }
            }
            Err(error) => return Err(error),
        }
    }
    Err(anyhow!("SSH retry budget exhausted"))
}

async fn connect(options: &ConnectionOptions) -> Result<(TcpListener, SshSession)> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, options.port))
        .await
        .context("could not bind local proxy on 127.0.0.1; choose a free --port")?;
    let ssh = SshSession::start(options).await?;
    Ok((listener, ssh))
}

async fn supervise(options: &ConnectionOptions, command: Option<&RunOptions>) -> Result<i32> {
    let mut interrupt = signal(SignalKind::interrupt())?;
    let mut terminate = signal(SignalKind::terminate())?;
    let cancel = CancellationToken::new();
    let (listener, mut ssh) = tokio::select! {
        _ = interrupt.recv() => return Ok(0),
        _ = terminate.recv() => return Ok(0),
        result = connect(options) => result?,
    };
    let port = listener.local_addr()?.port();
    let mut child = match command {
        Some(options) => Some(ManagedCommand::spawn(options, port)?),
        None => {
            terminal::exports(port)?;
            None
        }
    };
    let (available, connections) = watch::channel(Some(ssh.connection.clone()));
    let result = tokio::select! {
        _ = interrupt.recv() => Ok(0),
        _ = terminate.recv() => Ok(0),
        result = async { child.as_mut().unwrap().wait().await }, if child.is_some() => result,
        result = proxy::serve(listener, connections, cancel.clone()) => result.map(|_| 0),
        result = ssh.supervise(options, &available) => result,
    };
    cancel.cancel();
    available.send_replace(None);
    ssh.connection.close();
    drop(ssh);
    if let Some(child) = &mut child {
        child.shutdown().await?;
    }
    result
}
