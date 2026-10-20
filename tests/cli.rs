use std::process::Command;

#[test]
fn up_requires_an_ssh_server_and_user() {
    let output = Command::new(env!("CARGO_BIN_EXE_proxrs"))
        .arg("up")
        .output()
        .unwrap();
    assert!(!output.status.success());
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(error.contains("--host"));
    assert!(error.contains("--user"));
    assert!(output.stdout.is_empty());
}

#[test]
fn rejects_embedded_credentials_without_exposing_them() {
    for host_args in [
        vec!["--host", "alice:synthetic-test-secret@example.com"],
        vec!["--host=alice:synthetic-test-secret@example.com"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_proxrs"))
            .arg("up")
            .args(host_args)
            .args(["--user", "alice", "--password-stdin"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let error = String::from_utf8_lossy(&output.stderr);
        assert!(error.contains("host"));
        assert!(!error.contains("synthetic-test-secret"));
    }
}

#[test]
fn an_occupied_port_fails_without_waiting_for_a_password() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_proxrs"))
        .args([
            "up",
            "--host",
            "localhost",
            "--user",
            "alice",
            "--password-stdin",
            "--port",
        ])
        .arg(listener.local_addr().unwrap().port().to_string())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8_lossy(&output.stderr).contains("could not bind local proxy"));
}

#[test]
fn termination_cancels_a_stalled_password_pipe() {
    use std::{
        process::Stdio,
        thread,
        time::{Duration, Instant},
    };
    let reservation = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = reservation.local_addr().unwrap();
    drop(reservation);
    let mut child = Command::new(env!("CARGO_BIN_EXE_proxrs"))
        .args([
            "up",
            "--host",
            "localhost",
            "--user",
            "alice",
            "--password-stdin",
            "--port",
            &address.port().to_string(),
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let startup_deadline = Instant::now() + Duration::from_secs(5);
    while std::net::TcpStream::connect(address).is_err() {
        if Instant::now() >= startup_deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("proxy did not reach password input");
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert!(
        Command::new("kill")
            .args(["-TERM", &child.id().to_string()])
            .status()
            .unwrap()
            .success()
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("stalled password input prevented termination");
        }
        thread::sleep(Duration::from_millis(10));
    }
}
