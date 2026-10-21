// This module owns user-facing terminal output.
#![allow(clippy::disallowed_macros, clippy::disallowed_methods)]

use anyhow::{Context, Result, bail};
use std::{
    fs::{File, OpenOptions},
    io::{self, BufRead, IsTerminal, Read, Write},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
    path::Path,
    time::Duration,
};
use zeroize::Zeroizing;

pub fn approve_install(destination: &Path) -> Result<bool> {
    if !io::stdin().is_terminal() {
        bail!(
            "install destination {:?} already exists; replacement requires an interactive terminal or --force",
            destination
        );
    }
    eprint!("Replace {:?}? [y/N] ", destination);
    io::stderr()
        .flush()
        .context("could not display install prompt")?;
    let mut answer = String::new();
    io::stdin()
        .read_line(&mut answer)
        .context("could not read install approval")?;
    Ok(matches!(
        answer.trim().to_ascii_lowercase().as_str(),
        "y" | "yes"
    ))
}

pub fn installation_declined(destination: &Path) {
    eprintln!(
        "Installation cancelled; {:?} was left unchanged.",
        destination
    );
}

pub fn installation_complete(
    destination: &Path,
    status: &crate::install::PathStatus,
) -> Result<()> {
    use crate::install::PathStatus;

    eprintln!("Installed proxrs at {:?}", destination);
    match status {
        PathStatus::Ready => {
            eprintln!("proxrs is available on PATH.");
            return Ok(());
        }
        PathStatus::Missing => eprintln!("The install directory is not on PATH."),
        PathStatus::Shadowed(other) => {
            eprintln!("Another proxrs takes precedence on PATH: {:?}", other);
        }
    }
    let directory = destination
        .parent()
        .expect("install destination has a parent");
    let shell = std::env::var_os("SHELL");
    let name = shell
        .as_deref()
        .and_then(|shell| Path::new(shell).file_name())
        .and_then(|name| name.to_str())
        .unwrap_or("");
    let mut command = Vec::new();
    let startup_file = match name {
        "sh" | "bash" | "zsh" => {
            command.extend_from_slice(b"export PATH=");
            if name == "sh" {
                shell_quote::Sh::quote_into_vec(directory, &mut command);
            } else {
                shell_quote::Bash::quote_into_vec(directory, &mut command);
            }
            command.extend_from_slice(b"\"${PATH:+:$PATH}\"\n");
            match name {
                "zsh" => "your zsh startup file (usually ~/.zshrc)",
                "bash" => "your bash startup file (usually ~/.bashrc or ~/.bash_profile)",
                _ => "your shell's startup file",
            }
        }
        "fish" => {
            command.extend_from_slice(b"fish_add_path --path --prepend --move -- ");
            shell_quote::Fish::quote_into_vec(directory, &mut command);
            command.push(b'\n');
            "your fish startup file (usually ~/.config/fish/config.fish)"
        }
        _ => {
            eprintln!(
                "Shell detection from SHELL is unavailable or unsupported (supported: sh, bash, zsh, fish). Add {:?} to PATH using your shell's syntax.",
                directory
            );
            return Ok(());
        }
    };
    eprintln!("For {name} (selected by SHELL), run this in your current terminal:");
    let mut stdout = io::stdout().lock();
    stdout.write_all(&command)?;
    stdout.flush()?;
    eprintln!("For future terminals, add the same command to {startup_file}.");
    Ok(())
}

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

pub async fn password_from_terminal() -> Result<Zeroizing<String>> {
    read_terminal("SSH password: ", true)
        .await
        .context("could not read SSH password; use --password-stdin without a controlling terminal")
}

struct TerminalInput {
    file: File,
    original: Option<libc::termios>,
    complete: bool,
}

impl Drop for TerminalInput {
    fn drop(&mut self) {
        if let Some(original) = &self.original {
            // This guard owns an open terminal descriptor and the exact settings
            // read from that descriptor. Cancellation drops the guard as well.
            if !self.complete {
                // A shell may still own this terminal after cancellation. Do
                // not leave an unfinished secret for its next read command.
                unsafe { libc::tcflush(self.file.as_raw_fd(), libc::TCIFLUSH) };
            }
            unsafe { libc::tcsetattr(self.file.as_raw_fd(), libc::TCSANOW, original) };
            eprintln!();
        }
    }
}

async fn read_terminal(prompt: &str, secret: bool) -> Result<Zeroizing<String>> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOCTTY | libc::O_CLOEXEC)
        .open("/dev/tty")
        .context(
            "no controlling terminal; use --password-stdin for noninteractive authentication",
        )?;
    let mut terminal = TerminalInput {
        file,
        original: None,
        complete: false,
    };
    if secret {
        let mut settings = std::mem::MaybeUninit::<libc::termios>::uninit();
        // tcgetattr initializes the termios value only on success.
        if unsafe { libc::tcgetattr(terminal.file.as_raw_fd(), settings.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error()).context("could not read terminal settings");
        }
        let original = unsafe { settings.assume_init() };
        let mut hidden = original;
        hidden.c_lflag &= !(libc::ECHO | libc::ECHONL);
        terminal.original = Some(original);
        if unsafe { libc::tcsetattr(terminal.file.as_raw_fd(), libc::TCSANOW, &hidden) } != 0 {
            return Err(io::Error::last_os_error())
                .context("could not hide terminal password input");
        }
    }
    eprint!("{prompt}");
    io::stderr()
        .flush()
        .context("could not display terminal prompt")?;
    let mut line = Zeroizing::new(Vec::new());
    loop {
        let mut byte = [0];
        match terminal.file.read(&mut byte) {
            Ok(0) => bail!("terminal input ended before a response was provided"),
            Ok(_) if byte[0] == b'\n' => {
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                let answer = Zeroizing::new(
                    std::str::from_utf8(&line)
                        .context("terminal input is not valid UTF-8")?
                        .to_owned(),
                );
                terminal.complete = true;
                return Ok(answer);
            }
            Ok(_) => line.push(byte[0]),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                // macOS kqueue rejects the /dev/tty alias; nonblocking reads with
                // a short async pause remain cancellable without a blocking task.
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error).context("could not read terminal input"),
        }
    }
}

pub async fn approve_host(host: &str, port: u16, key_type: &str, fingerprint: &str) -> Result<()> {
    eprintln!(
        "Unknown SSH host {host}, port {port}\nKey type: {key_type}\nSHA-256 fingerprint: {fingerprint}"
    );
    let answer = read_terminal("Trust this key and save it? [yes/NO] ", false).await
        .context("unknown SSH host key; noninteractive operation requires pretrusted known-hosts storage")?;
    if !answer.trim().eq_ignore_ascii_case("yes") {
        bail!("SSH host key approval refused");
    }
    Ok(())
}

pub fn destination_failure(error: &crate::transport::DestinationError) {
    eprintln!("{error}");
}

pub fn ssh_connection_lost() {
    eprintln!("proxrs: SSH connection lost; interrupted requests remain failed");
}

pub fn ssh_connection_restored() {
    eprintln!("proxrs: SSH connection restored; new requests can proceed");
}

pub fn ssh_retry(attempt: u32, delay: u64) {
    eprintln!("proxrs: SSH connection unavailable; retry {attempt}/3 in {delay} seconds");
}

pub fn restoration_failure(error: &io::Error) {
    eprintln!("proxrs: could not restore terminal: {error}");
}

pub fn session_failure(error: &anyhow::Error) {
    eprintln!("proxrs: {error:#}");
}
