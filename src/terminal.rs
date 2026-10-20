use anyhow::{Context, Result, bail};
use std::io::{self, BufRead, Write};
use zeroize::Zeroizing;

pub async fn password_from_stdin() -> Result<Zeroizing<String>> {
    let (sender, receiver) = tokio::sync::oneshot::channel();
    // A dedicated thread can be abandoned on cancellation. Tokio's blocking pool
    // would hold runtime shutdown open forever when a password pipe stalls.
    std::thread::spawn(move || {
        let mut password = Zeroizing::new(String::new());
        let result = io::stdin().lock().read_line(&mut password).map(|_| {
            if password.ends_with('\n') {
                password.pop();
                if password.ends_with('\r') {
                    password.pop();
                }
            }
            password
        });
        let _ = sender.send(result);
    });
    receiver
        .await
        .context("password reader stopped")?
        .context("could not read password from stdin")
}

pub fn exports(port: u16) -> Result<()> {
    let mut stdout = io::stdout().lock();
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"] {
        writeln!(stdout, "export {name}='http://127.0.0.1:{port}'")?;
    }
    for name in ["NO_PROXY", "no_proxy"] {
        writeln!(stdout, "export {name}='localhost,127.0.0.1,::1'")?;
    }
    stdout.flush()?;
    eprintln!("Proxy ready. Apply these exports in a second shell; keep proxrs running.");
    Ok(())
}

pub fn require_stdin(enabled: bool) -> Result<()> {
    if !enabled {
        bail!("interactive password entry is not available yet; supply --password-stdin");
    }
    Ok(())
}
