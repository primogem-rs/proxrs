#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use log::debug;
    use ssh_key::PrivateKey;
    use tokio::net::TcpListener;

    use crate::cert::PublicKeyOrCertificate;
    // Import client types directly since we're in the client module
    use crate::Error;
    use crate::client::{Config, Handler, connect};
    use crate::keys::PrivateKeyWithHashAlg;
    use crate::server::{self, Auth, Handler as ServerHandler, Server, Session};
    use crate::{ChannelId, SshId}; // Import directly from crate root
    use rand::rng;

    #[derive(Clone)]
    struct TestServer {
        clients: Arc<Mutex<HashMap<(usize, ChannelId), server::Handle>>>,
        id: usize,
    }

    impl server::Server for TestServer {
        type Handler = Self;

        fn new_client(&mut self, _: Option<std::net::SocketAddr>) -> Self {
            let s = self.clone();
            self.id += 1;
            s
        }
    }

    impl ServerHandler for TestServer {
        type Error = Error;

        async fn channel_open_session(
            &mut self,
            channel: crate::channels::Channel<server::Msg>,
            reply: server::ChannelOpenHandle,
            session: &mut Session,
        ) -> Result<(), Self::Error> {
            {
                let mut clients = self.clients.lock().unwrap();
                clients.insert((self.id, channel.id()), session.handle());
            }
            reply.accept().await;
            Ok(())
        }

        async fn auth_publickey(
            &mut self,
            _: &str,
            _: &ssh_key::PublicKey,
        ) -> Result<Auth, Self::Error> {
            debug!("auth_publickey");
            Ok(Auth::Accept)
        }

        async fn data(
            &mut self,
            channel: ChannelId,
            data: &[u8],
            session: &mut Session,
        ) -> Result<(), Self::Error> {
            debug!("server received data: {:?}", std::str::from_utf8(data));
            session.data(channel, data.to_vec())?;
            Ok(())
        }

        async fn tcpip_forward(
            &mut self,
            _: &str,
            _: &mut u32,
            _: &mut Session,
        ) -> Result<bool, Self::Error> {
            Ok(true)
        }
    }

    struct Client {}

    impl Handler for Client {
        type Error = Error;

        async fn check_server_key(
            &mut self,
            _: &PublicKeyOrCertificate,
        ) -> Result<bool, Self::Error> {
            Ok(true)
        }
    }

    struct PasswordResultServer {
        disconnect: bool,
        auth_seen: Arc<tokio::sync::Notify>,
    }

    impl ServerHandler for PasswordResultServer {
        type Error = Error;

        async fn auth_password(&mut self, _: &str, _: &str) -> Result<Auth, Error> {
            self.auth_seen.notify_one();
            if self.disconnect {
                Err(Error::Disconnect)
            } else {
                Ok(Auth::Reject {
                    proceed_with_methods: Some(crate::MethodSet::empty()),
                    partial_success: false,
                })
            }
        }
    }

    struct PasswordResultClient(Arc<tokio::sync::Notify>);

    impl Handler for PasswordResultClient {
        type Error = Error;

        async fn check_server_key(&mut self, _: &PublicKeyOrCertificate) -> Result<bool, Error> {
            Ok(true)
        }

        async fn disconnected(
            &mut self,
            _: crate::client::DisconnectReason<Error>,
        ) -> Result<(), Error> {
            self.0.notify_one();
            Ok(())
        }
    }

    #[tokio::test]
    async fn password_auth_distinguishes_rejection_from_transport_loss() {
        use std::future::{Future, poll_fn};
        use std::task::Poll;

        for disconnect in [false, true] {
            let mut config = server::Config::default();
            config.auth_rejection_time = std::time::Duration::ZERO;
            config
                .keys
                .push(PrivateKey::random(&mut rng(), ssh_key::Algorithm::Ed25519).unwrap());
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap();
            let server = tokio::spawn(async move {
                let (socket, _) = listener.accept().await.unwrap();
                let auth_seen = Arc::new(tokio::sync::Notify::new());
                let session = server::run_stream(
                    Arc::new(config),
                    socket,
                    PasswordResultServer {
                        disconnect,
                        auth_seen: auth_seen.clone(),
                    },
                )
                .await
                .unwrap();
                auth_seen.notified().await;
                if !disconnect {
                    // This queued disconnect follows the explicit failure reply.
                    session
                        .handle()
                        .disconnect(
                            crate::Disconnect::ByApplication,
                            "finished".into(),
                            "".into(),
                        )
                        .await
                        .unwrap();
                }
                let _ = session.await;
            });
            let closed = Arc::new(tokio::sync::Notify::new());
            let mut client = connect(
                Arc::new(Config::default()),
                addr,
                PasswordResultClient(closed.clone()),
            )
            .await
            .unwrap();
            {
                let result = client.authenticate_password("user", "disposable-test-password");
                tokio::pin!(result);
                // Send the public auth request, then wait for the driver to end
                // before consuming its queued reply. A real rejection must not
                // become transport loss merely because the driver is closed.
                poll_fn(|cx| {
                    assert!(result.as_mut().poll(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
                tokio::time::timeout(std::time::Duration::from_secs(3), closed.notified())
                    .await
                    .unwrap();
                let result = result.await;
                if disconnect {
                    assert!(matches!(result, Err(Error::RecvError)));
                } else {
                    assert!(matches!(result, Ok(crate::client::AuthResult::Failure {
                        remaining_methods, partial_success: false,
                    }) if remaining_methods.is_empty()));
                }
            }
            drop(client);
            tokio::time::timeout(std::time::Duration::from_secs(3), server)
                .await
                .unwrap()
                .unwrap();
        }
    }

    #[tokio::test]
    async fn test_client_connects_to_protocol_1_99() {
        let _ = env_logger::try_init();

        // Create a client key
        let client_key = PrivateKey::random(&mut rng(), ssh_key::Algorithm::Ed25519).unwrap();

        // Configure the server
        let mut config = server::Config::default();
        config.auth_rejection_time = std::time::Duration::from_secs(1);
        config.server_id = SshId::Standard("SSH-1.99-CustomServer_1.0".into());
        config.inactivity_timeout = None;
        config
            .keys
            .push(PrivateKey::random(&mut rng(), ssh_key::Algorithm::Ed25519).unwrap());
        let config = Arc::new(config);

        // Create server struct
        let mut server = TestServer {
            clients: Arc::new(Mutex::new(HashMap::new())),
            id: 0,
        };

        // Start the TCP listener for our mock server
        let socket = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = socket.local_addr().unwrap();

        // Spawn a separate task that will handle the server connection
        tokio::spawn(async move {
            // Accept a connection
            let (socket, _) = socket.accept().await.unwrap();

            // Handle the connection with the server
            let server_handler = server.new_client(None);
            server::run_stream(config, socket, server_handler)
                .await
                .unwrap();
        });

        println!("Server listening on {addr}");

        // Configure the client
        let client_config = Arc::new(Config::default());

        // Connect to the server
        let mut session = connect(client_config, addr, Client {}).await.unwrap();

        // Unfortunately, we can't directly verify the protocol version from the client API
        // The Protocol199Stream wrapper ensures the server sends SSH-1.99-CustomServer_1.0
        // The test passing means the client accepted this protocol version

        // Try to authenticate
        let auth_result = session
            .authenticate_publickey(
                std::env::var("USER").unwrap_or("user".to_string()),
                PrivateKeyWithHashAlg::new(
                    Arc::new(client_key),
                    session.best_supported_rsa_hash().await.unwrap().flatten(),
                ),
            )
            .await
            .unwrap();

        assert!(auth_result.success());

        // Try opening a session channel
        let mut channel = session.channel_open_session().await.unwrap();

        // Send some data
        let test_data = b"Hello, 1.99 protocol server!";
        channel.data(&test_data[..]).await.unwrap();

        // Wait for response
        let msg = channel.wait().await.unwrap();
        match msg {
            crate::channels::ChannelMsg::Data { data: msg_data } => {
                assert_eq!(test_data.as_slice(), &msg_data[..]);
            }
            msg => panic!("Unexpected message {msg:?}"),
        }
    }

    #[tokio::test]
    async fn test_send_custom_global_request() {
        let _ = env_logger::try_init();

        let client_key = PrivateKey::random(&mut rng(), ssh_key::Algorithm::Ed25519).unwrap();

        let mut config = server::Config::default();
        config.auth_rejection_time = std::time::Duration::from_secs(1);
        config.inactivity_timeout = None;
        config
            .keys
            .push(PrivateKey::random(&mut rng(), ssh_key::Algorithm::Ed25519).unwrap());
        let config = Arc::new(config);

        let mut server = TestServer {
            clients: Arc::new(Mutex::new(HashMap::new())),
            id: 0,
        };

        let socket = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = socket.local_addr().unwrap();

        tokio::spawn(async move {
            let (socket, _) = socket.accept().await.unwrap();
            let server_handler = server.new_client(None);
            server::run_stream(config, socket, server_handler)
                .await
                .unwrap();
        });

        let client_config = Arc::new(Config::default());
        let mut session = connect(client_config, addr, Client {}).await.unwrap();

        let auth_result = session
            .authenticate_publickey(
                std::env::var("USER").unwrap_or("user".to_string()),
                PrivateKeyWithHashAlg::new(
                    Arc::new(client_key),
                    session.best_supported_rsa_hash().await.unwrap().flatten(),
                ),
            )
            .await
            .unwrap();
        assert!(auth_result.success());

        // The default server has no handler for this request name, so it replies
        // with a failure, which surfaces as RequestDenied.
        let denied = session
            .send_global_request("custom-request@example.com", b"payload", true)
            .await;
        assert!(matches!(denied, Err(Error::RequestDenied)));

        // Without a reply requested the call returns immediately and the server
        // must stay silent. Queue a reply-wanting request right behind it: an
        // unsolicited failure for the first request would be matched to the
        // forward and turn its success into RequestDenied.
        let no_reply = session
            .send_global_request("custom-request@example.com", b"payload", false)
            .await
            .unwrap();
        assert!(no_reply.is_none());
        session.tcpip_forward("127.0.0.1", 12345).await.unwrap();
    }
}
