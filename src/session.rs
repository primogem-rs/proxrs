use crate::{cli::ConnectionOptions, proxy, terminal, transport::Connection};
use anyhow::{Context, Result, bail};
use std::{sync::Arc, time::Duration};
use tokio::{
    net::TcpListener,
    signal::unix::{SignalKind, signal},
};
use tokio_util::sync::CancellationToken;

pub async fn up(options: ConnectionOptions) -> Result<()> {
    let mut interrupt = signal(SignalKind::interrupt())?;
    let mut terminate = signal(SignalKind::terminate())?;
    let cancel = CancellationToken::new();
    let result = tokio::select! {
        _ = interrupt.recv() => Ok(()),
        _ = terminate.recv() => Ok(()),
        result = run(options, cancel.clone()) => result,
    };
    cancel.cancel();
    result
}

async fn run(options: ConnectionOptions, cancel: CancellationToken) -> Result<()> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, options.port))
        .await
        .context("could not bind local proxy on 127.0.0.1; choose a free --port")?;
    let port = listener.local_addr()?.port();
    let mut password = if options.password_stdin {
        Some(terminal::password_from_stdin().await?)
    } else {
        None
    };
    let ssh = Arc::new(Connection::connect(&options, &mut password).await?);
    drop(password);
    terminal::exports(port)?;
    tokio::select! {
        result = proxy::serve(listener, ssh.clone(), cancel) => result,
        _ = async { while !ssh.is_closed() { tokio::time::sleep(Duration::from_millis(100)).await; } } => bail!("SSH connection lost; proxy session stopped"),
    }
}
