use clap::{Args, Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    version,
    about = "A loopback HTTP proxy through password-authenticated SSH"
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Subcommand)]
pub enum Command {
    /// Copy this executable into a user bin directory and show any PATH setup.
    Install(InstallOptions),
    /// Keep a local proxy running; use the printed exports in a second shell.
    Up(ConnectionOptions),
    /// Run a foreground command with the proxy environment and own its lifetime.
    Run(RunOptions),
}

#[derive(Args)]
pub struct InstallOptions {
    /// Install into this directory instead of ~/.local/bin.
    #[arg(long, value_name = "DIRECTORY")]
    pub bin_dir: Option<PathBuf>,
    /// Replace an existing executable without prompting.
    #[arg(long)]
    pub force: bool,
}

#[derive(Args)]
pub struct RunOptions {
    #[command(flatten)]
    pub connection: ConnectionOptions,
    #[arg(long)]
    pub inherit_no_proxy: bool,
    #[arg(last = true, required = true, num_args = 1..)]
    pub command: Vec<std::ffi::OsString>,
}

#[derive(Args)]
pub struct ConnectionOptions {
    #[arg(long, value_parser = HostParser)]
    pub host: String,
    #[arg(long, value_parser = clap::builder::NonEmptyStringValueParser::new())]
    pub user: String,
    #[arg(long, default_value_t = 22, value_parser = clap::value_parser!(u16).range(1..))]
    pub ssh_port: u16,
    #[arg(long, default_value_t = 8080)]
    pub port: u16,
    #[arg(long)]
    pub password_stdin: bool,
    #[arg(long)]
    pub known_hosts: Option<PathBuf>,
}

impl ConnectionOptions {
    pub fn known_hosts_path(&self) -> anyhow::Result<PathBuf> {
        match &self.known_hosts {
            Some(path) => Ok(path.clone()),
            None => Ok(PathBuf::from(
                std::env::var_os("HOME")
                    .ok_or_else(|| anyhow::anyhow!("HOME is unset; provide --known-hosts"))?,
            )
            .join(".ssh/known_hosts")),
        }
    }
}

#[derive(Clone)]
struct HostParser;

impl clap::builder::TypedValueParser for HostParser {
    type Value = String;

    fn parse_ref(
        &self,
        command: &clap::Command,
        _argument: Option<&clap::Arg>,
        value: &std::ffi::OsStr,
    ) -> Result<String, clap::Error> {
        value
            .to_str()
            .and_then(|value| parse_host(value).ok())
            .ok_or_else(|| {
                // Function-based parsers make Clap include the rejected value.
                // It may itself contain credentials, so report only the rule.
                clap::Error::raw(
                    clap::error::ErrorKind::ValueValidation,
                    "invalid --host: host must be a DNS name or IP address without credentials or a port",
                )
                .with_cmd(command)
            })
    }
}

fn parse_host(value: &str) -> Result<String, String> {
    if value.parse::<std::net::IpAddr>().is_ok() {
        return Ok(value.to_owned());
    }
    let hostname = value.strip_suffix('.').unwrap_or(value);
    if hostname.is_empty()
        || hostname.len() > 253
        || hostname.split('.').any(|label| {
            label.is_empty()
                || label.len() > 63
                || label.starts_with('-')
                || label.ends_with('-')
                || !label
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        })
    {
        return Err(
            "host must be a DNS name or IP address without credentials or a port".to_owned(),
        );
    }
    Ok(value.to_owned())
}
