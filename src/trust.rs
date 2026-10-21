use anyhow::{Context, Result, bail};
use hmac::{Hmac, KeyInit, Mac};
use sha1::Sha1;
use ssh_key::{
    PublicKey,
    encoding::base64::{Base64, Encoding},
    known_hosts::{HostPatterns, KnownHosts, Marker},
};
use std::{
    fs::{DirBuilder, OpenOptions},
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::Path,
};

pub fn verify(path: &Path, host: &str, port: u16, key: &[u8]) -> Result<bool> {
    let contents = read_store(path)?;
    // Normalize field separators for the maintained known-host parser, while
    // preserving the original file byte-for-byte when appending approved keys.
    let normalized = contents
        .lines()
        .map(|line| line.split_ascii_whitespace().collect::<Vec<_>>().join(" "))
        .collect::<Vec<_>>()
        .join("\n");
    validate_hash_lengths(&normalized)?;
    let server_key =
        PublicKey::from_bytes(key).context("unsupported SSH host key or certificate")?;
    let name = host_identity(host, port);
    let mut applicable = false;
    let mut matched = false;
    for entry in KnownHosts::new(&normalized) {
        let entry = entry.context("malformed known-hosts storage")?;
        let matches = match entry.host_patterns() {
            HostPatterns::HashedName { salt, hash } => {
                if salt.len() != 20 {
                    bail!("malformed known-hosts hash salt");
                }
                Hmac::<Sha1>::new_from_slice(salt)
                    .expect("HMAC accepts all key lengths")
                    .chain_update(name.as_bytes())
                    .verify_slice(hash)
                    .is_ok()
            }
            HostPatterns::Patterns(patterns) => match_patterns(patterns, &name)?,
        };
        if !matches {
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
        return Ok(true);
    }
    if applicable {
        bail!("SSH host key changed; verify the server identity before updating known hosts");
    }
    Ok(false)
}

fn validate_hash_lengths(contents: &str) -> Result<()> {
    // The upstream parser decodes into a fixed-size array but does not reject a
    // short decoded hash. Validate the original field before that padding can
    // turn malformed storage into an apparently unknown host.
    for line in contents.lines().filter(|line| !line.starts_with('#')) {
        let mut fields = line.split_ascii_whitespace();
        let first = fields.next().unwrap_or_default();
        let hosts = if first.starts_with('@') {
            fields.next().unwrap_or_default()
        } else {
            first
        };
        if let Some((_, encoded)) = hosts
            .strip_prefix("|1|")
            .and_then(|hash| hash.split_once('|'))
            && Base64::decode_vec(encoded)
                .context("malformed known-hosts hash")?
                .len()
                != 20
        {
            bail!("malformed known-hosts hash length");
        }
    }
    Ok(())
}

fn read_store(path: &Path) -> Result<String> {
    match std::fs::read_to_string(path) {
        Ok(contents) => Ok(contents),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(error).context("could not read known-hosts storage"),
    }
}

pub fn save(path: &Path, host: &str, port: u16, key: &PublicKey) -> Result<()> {
    // Recheck after the human prompt: don't replace trust changed meanwhile.
    if verify(path, host, port, &key.to_bytes()?)? {
        return Ok(());
    }
    let contents = read_store(path)?;
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(parent)
            .context("could not create known-hosts directory")?;
    }
    let mut file = OpenOptions::new()
        .append(true)
        .create(true)
        .mode(0o600)
        .open(path)
        .context("could not save approved SSH host key")?;
    if !contents.is_empty() && !contents.ends_with('\n') {
        writeln!(file)?;
    }
    let name = host_identity(host, port);
    writeln!(file, "{name} {}", key.to_openssh()?)
        .context("could not save approved SSH host key")?;
    file.flush().context("could not save approved SSH host key")
}

fn host_identity(host: &str, port: u16) -> String {
    if port == 22 {
        host.to_ascii_lowercase()
    } else {
        format!("[{}]:{port}", host.to_ascii_lowercase())
    }
}

fn match_patterns(patterns: &[String], name: &str) -> Result<bool> {
    let mut matched = false;
    let mut excluded = false;
    for pattern in patterns {
        let (negated, pattern) = pattern
            .strip_prefix('!')
            .map_or((false, pattern.as_str()), |pattern| (true, pattern));
        if pattern.is_empty() || pattern.contains('|') {
            bail!("malformed known-hosts hostname pattern");
        }
        if wildcard_matches(pattern.as_bytes(), name.as_bytes()) {
            if negated {
                excluded = true;
            } else {
                matched = true;
            }
        }
    }
    Ok(matched && !excluded)
}

// OpenSSH hostname patterns have only '*' and '?' wildcards. Brackets around
// IPv6 addresses and nondefault ports are literal, not filesystem glob classes.
fn wildcard_matches(pattern: &[u8], name: &[u8]) -> bool {
    let (mut p, mut n) = (0, 0);
    let (mut star, mut retry_at) = (None, 0);
    while n < name.len() {
        if p < pattern.len() && (pattern[p] == b'?' || pattern[p].eq_ignore_ascii_case(&name[n])) {
            p += 1;
            n += 1;
        } else if p < pattern.len() && pattern[p] == b'*' {
            star = Some(p);
            p += 1;
            retry_at = n;
        } else if let Some(previous) = star {
            retry_at += 1;
            n = retry_at;
            p = previous + 1;
        } else {
            return false;
        }
    }
    pattern[p..].iter().all(|byte| *byte == b'*')
}
