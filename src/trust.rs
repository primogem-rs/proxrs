use anyhow::{Context, Result, bail};
use ssh_key::{
    PublicKey,
    known_hosts::{HostPatterns, KnownHosts, Marker},
};
use std::path::Path;

pub fn verify(path: &Path, host: &str, port: u16, key: &[u8]) -> Result<()> {
    let contents = std::fs::read_to_string(path).context("could not read known-hosts storage")?;
    let server_key =
        PublicKey::from_bytes(key).context("unsupported SSH host key or certificate")?;
    let name = if port == 22 {
        host.to_owned()
    } else {
        format!("[{host}]:{port}")
    };
    let mut applicable = false;
    let mut matched = false;
    for entry in KnownHosts::new(&contents) {
        let entry = entry.context("malformed known-hosts storage")?;
        let HostPatterns::Patterns(patterns) = entry.host_patterns() else {
            bail!("hashed known-host records are not supported in this initial slice");
        };
        if patterns
            .iter()
            .any(|pattern| pattern.contains(['*', '?']) || pattern.starts_with('!'))
        {
            bail!("known-host patterns are not supported in this initial slice");
        }
        if !patterns
            .iter()
            .any(|pattern| pattern.eq_ignore_ascii_case(&name))
        {
            continue;
        }
        applicable = true;
        if entry.marker() == Some(&Marker::CertAuthority) {
            bail!("SSH host-certificate authority trust is unsupported");
        }
        if entry.public_key().key_data() == server_key.key_data() {
            if entry.marker() == Some(&Marker::Revoked) {
                bail!("SSH host key is revoked");
            }
            matched = true;
        }
    }
    if matched {
        return Ok(());
    }
    if applicable {
        bail!("SSH host key changed; verify the server identity before updating known hosts");
    }
    bail!("unknown SSH host key; establish trusted known-hosts storage before noninteractive use")
}
