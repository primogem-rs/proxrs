mod cli;
mod client_connection;
mod install;
mod managed;
mod proxy;
mod session;
mod terminal;
mod transport;
mod trust;
use clap::Parser;

#[tokio::main]
async fn main() {
    let result = match cli::Cli::parse().command {
        cli::Command::Install(options) => install::install(options),
        cli::Command::Up(options) => session::up(options).await,
        cli::Command::Run(options) => session::managed(options).await,
    };
    match result {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            terminal::session_failure(&error);
            std::process::exit(1);
        }
    }
}
