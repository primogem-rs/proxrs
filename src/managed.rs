use crate::{cli::RunOptions, terminal};
use anyhow::{Context, Result};
use std::{
    fs::{File, OpenOptions},
    io,
    os::{
        fd::AsRawFd,
        unix::{
            fs::OpenOptionsExt,
            process::{CommandExt, ExitStatusExt},
        },
    },
    process::Stdio,
};
use tokio::process::{Child, Command};

pub struct ManagedCommand {
    child: Child,
    group: Option<libc::pid_t>,
    _terminal: Option<ForegroundTerminal>,
}

impl ManagedCommand {
    pub fn spawn(options: &RunOptions, port: u16) -> Result<Self> {
        let mut command = Command::new(&options.command[0]);
        command.args(&options.command[1..]);
        let proxy = format!("http://127.0.0.1:{port}");
        for name in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"] {
            command.env(name, &proxy);
        }
        let mut exclusions = if options.inherit_no_proxy {
            std::env::var_os("no_proxy")
                .or_else(|| std::env::var_os("NO_PROXY"))
                .unwrap_or_default()
        } else {
            std::ffi::OsString::new()
        };
        if exclusions != "*" {
            if !exclusions.is_empty() {
                exclusions.push(",");
            }
            exclusions.push("localhost,127.0.0.1,::1");
        }
        for name in ["NO_PROXY", "no_proxy"] {
            command.env(name, &exclusions);
        }
        command.env_remove("ALL_PROXY").env_remove("all_proxy");
        command.process_group(0);
        let terminal = ForegroundTerminal::open()?;
        if options.connection.password_stdin {
            command.stdin(match &terminal {
                Some(terminal) => Stdio::from(terminal.file.try_clone()?),
                None => Stdio::null(),
            });
        }
        if let Some(terminal) = &terminal {
            let fd = terminal.file.as_raw_fd();
            // The child has its process group before pre_exec. Handoff here
            // avoids both a fast-child SIGTTIN race and an exec-ack deadlock.
            // Only async-signal-safe libc calls run between fork and exec.
            unsafe {
                command.as_std_mut().pre_exec(move || {
                    with_job_control_blocked(|| {
                        if libc::tcsetpgrp(fd, libc::getpgrp()) == -1 {
                            Err(io::Error::last_os_error())
                        } else {
                            Ok(())
                        }
                    })
                });
            }
        }
        let child = command
            .spawn()
            .context("could not launch managed command")?;
        let group = child.id().expect("newly spawned command has a process ID") as libc::pid_t;
        assert!(
            group > 1,
            "managed process group must never address unrelated processes"
        );
        Ok(Self {
            group: Some(group),
            child,
            _terminal: terminal,
        })
    }

    pub async fn wait(&mut self) -> Result<i32> {
        let status = self
            .child
            .wait()
            .await
            .context("could not wait for managed command")?;
        Ok(status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or(1)))
    }

    pub async fn shutdown(&mut self) -> Result<()> {
        let Some(group) = self.group else {
            return Ok(());
        };
        signal_group(group, libc::SIGTERM)?;
        let grace = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
        loop {
            self.child
                .try_wait()
                .context("could not reap managed command")?;
            if !group_exists(group)? {
                self.group = None;
                return Ok(());
            }
            if tokio::time::Instant::now() >= grace {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        signal_group(group, libc::SIGKILL)?;
        self.child
            .wait()
            .await
            .context("could not reap terminated managed command")?;
        // SIGKILL is asynchronous, including for descendants reaped by the OS.
        let killed = tokio::time::Instant::now() + std::time::Duration::from_secs(2);
        while group_exists(group)? {
            anyhow::ensure!(
                tokio::time::Instant::now() < killed,
                "managed process group did not exit after SIGKILL"
            );
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        self.group = None;
        Ok(())
    }
}

impl Drop for ManagedCommand {
    fn drop(&mut self) {
        if let Some(group) = self.group {
            // Exceptional unwinding must not leave a command detached from its
            // proxy. Normal exits use the asynchronous five-second grace above.
            let _ = signal_group(group, libc::SIGKILL);
        }
    }
}

fn signal_group(group: libc::pid_t, signal: libc::c_int) -> Result<()> {
    // group is the positive PID of the command we created, never 0 or 1.
    if unsafe { libc::kill(-group, signal) } == -1 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("could not signal managed process group");
        }
    }
    Ok(())
}

fn group_exists(group: libc::pid_t) -> Result<bool> {
    if unsafe { libc::kill(-group, 0) } == 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(false)
    } else {
        Err(error).context("could not inspect managed process group")
    }
}

struct ForegroundTerminal {
    file: File,
    group: libc::pid_t,
    settings: libc::termios,
}

impl ForegroundTerminal {
    fn open() -> Result<Option<Self>> {
        let file = match OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NOCTTY | libc::O_CLOEXEC)
            .open("/dev/tty")
        {
            Ok(file) => file,
            Err(error)
                if matches!(
                    error.raw_os_error(),
                    Some(libc::ENXIO | libc::ENODEV | libc::ENOENT)
                ) =>
            {
                return Ok(None);
            }
            Err(error) => return Err(error).context("could not open controlling terminal"),
        };
        let mut settings = std::mem::MaybeUninit::uninit();
        // A valid owned terminal descriptor is used throughout this guard.
        let group = unsafe { libc::tcgetpgrp(file.as_raw_fd()) };
        if group == -1 || unsafe { libc::tcgetattr(file.as_raw_fd(), settings.as_mut_ptr()) } == -1
        {
            return Err(io::Error::last_os_error())
                .context("could not save terminal ownership and settings");
        }
        Ok(Some(Self {
            file,
            group,
            settings: unsafe { settings.assume_init() },
        }))
    }
}

impl Drop for ForegroundTerminal {
    fn drop(&mut self) {
        // Proxrs may currently be a background group. Block SIGTTOU only on
        // this thread while restoring exactly the previous foreground state.
        let restored = unsafe {
            with_job_control_blocked(|| {
                if libc::tcsetpgrp(self.file.as_raw_fd(), self.group) == -1
                    || libc::tcsetattr(self.file.as_raw_fd(), libc::TCSANOW, &self.settings) == -1
                {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(())
                }
            })
        };
        if let Err(error) = restored {
            terminal::restoration_failure(&error);
        }
    }
}

// Caller must use only async-signal-safe operations in action when called from
// pre_exec. The prior mask is restored before the command executes.
unsafe fn with_job_control_blocked(action: impl FnOnce() -> io::Result<()>) -> io::Result<()> {
    unsafe {
        let mut blocked = std::mem::zeroed();
        let mut previous = std::mem::zeroed();
        libc::sigemptyset(&mut blocked);
        libc::sigaddset(&mut blocked, libc::SIGTTOU);
        if libc::sigprocmask(libc::SIG_BLOCK, &blocked, &mut previous) == -1 {
            return Err(io::Error::last_os_error());
        }
        let result = action();
        if libc::sigprocmask(libc::SIG_SETMASK, &previous, std::ptr::null_mut()) == -1 {
            return Err(io::Error::last_os_error());
        }
        result
    }
}
