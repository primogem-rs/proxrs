use crate::{client_connection, transport::Connection};
use std::sync::Arc;
use tokio::{
    net::TcpListener,
    sync::{Semaphore, watch},
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
                clients.spawn(async move {
                    let _permit = permit;
                    tokio::select! {
                        _ = cancel.cancelled() => {},
                        _ = client_connection::serve(stream, ssh) => {},
                    }
                });
            }
        }
    }
    clients.abort_all();
    while clients.join_next().await.is_some() {}
    Ok(())
}
