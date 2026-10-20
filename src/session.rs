use crate::{
    cli::{ConnectionOptions, RunOptions},
    managed::ManagedCommand,
    proxy, terminal,
    transport::Connection,
};
use anyhow::{Context, Result, anyhow};
use std::{sync::Arc, time::Duration};
use tokio::{
    net::TcpListener,
    signal::unix::{SignalKind, signal},
};
use tokio_util::sync::CancellationToken;

pub async fn up(options: ConnectionOptions) -> Result<i32> {
    supervise(&options, None).await
}

pub async fn managed(options: RunOptions) -> Result<i32> {
    supervise(&options.connection, Some(&options)).await
}

async fn connect(options: &ConnectionOptions) -> Result<(TcpListener, Arc<Connection>)> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, options.port))
        .await
        .context("could not bind local proxy on 127.0.0.1; choose a free --port")?;
    let mut password = if options.password_stdin {
        Some(terminal::password_from_stdin().await?)
    } else {
        None
    };
    let ssh = Arc::new(Connection::connect(options, &mut password).await?);
    Ok((listener, ssh))
}

async fn supervise(options: &ConnectionOptions, command: Option<&RunOptions>) -> Result<i32> {
    let mut interrupt = signal(SignalKind::interrupt())?;
    let mut terminate = signal(SignalKind::terminate())?;
    let cancel = CancellationToken::new();
    let (listener, ssh) = tokio::select! {
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
    let result = tokio::select! {
        _ = interrupt.recv() => Ok(0),
        _ = terminate.recv() => Ok(0),
        result = async { child.as_mut().unwrap().wait().await }, if child.is_some() => result,
        result = proxy::serve(listener, ssh.clone(), cancel.clone()) => result.map(|_| 0),
        _ = async { while !ssh.is_closed() { tokio::time::sleep(Duration::from_millis(100)).await; } } => Err(anyhow!("SSH connection lost; proxy session stopped")),
    };
    cancel.cancel();
    drop(ssh);
    if let Some(child) = &mut child {
        child.shutdown().await?;
    }
    result
}
