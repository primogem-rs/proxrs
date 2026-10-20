mod cli;
mod http_ingress;
mod proxy;
mod session;
mod terminal;
mod transport;
mod trust;
use clap::Parser;

#[tokio::main]
async fn main() {
    let result = match cli::Cli::parse().command {
        cli::Command::Up(options) => session::up(options).await,
    };
    if let Err(error) = result {
        eprintln!("proxrs: {error:#}");
        std::process::exit(1);
    }
}
