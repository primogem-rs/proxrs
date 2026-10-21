use anyhow::{Context, Result, ensure};
use std::{
    env,
    ffi::CString,
    fs::{self, File},
    io,
    os::unix::{ffi::OsStrExt, fs::PermissionsExt},
    path::{Path, PathBuf},
};

use crate::{cli::InstallOptions, terminal};

pub enum PathStatus {
    Ready,
    Missing,
    Shadowed(PathBuf),
}

pub fn install(options: InstallOptions) -> Result<i32> {
    let directory = match options.bin_dir {
        Some(directory) => directory,
        None => PathBuf::from(
            env::var_os("HOME")
                .filter(|home| !home.is_empty())
                .context("HOME is unset or empty; provide --bin-dir")?,
        )
        .join(".local/bin"),
    };
    ensure!(
        !directory.as_os_str().as_bytes().contains(&b':'),
        "the install directory contains ':' and cannot be represented in PATH; choose another --bin-dir"
    );
    fs::create_dir_all(&directory)
        .with_context(|| format!("could not create install directory {:?}", directory))?;
    let directory = directory
        .canonicalize()
        .context("could not resolve install directory")?;
    ensure!(
        !directory.as_os_str().as_bytes().contains(&b':'),
        "the resolved install directory contains ':' and cannot be represented in PATH; choose another --bin-dir"
    );
    let destination = directory.join("proxrs");
    let replacing = match fs::symlink_metadata(&destination) {
        Ok(metadata) => {
            ensure!(
                !metadata.is_dir(),
                "install destination is a directory: {:?}",
                destination
            );
            true
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => return Err(error).context("could not inspect install destination"),
    };
    if replacing && !options.force && !terminal::approve_install(&destination)? {
        terminal::installation_declined(&destination);
        return Ok(0);
    }

    let source = env::current_exe().context("could not locate the invoked executable")?;
    let mut source = File::open(source).context("could not open the invoked executable")?;
    // Stage in the destination directory so publishing the completed executable
    // stays on the same filesystem and never truncates a running or linked file.
    let mut staged = tempfile::Builder::new()
        .prefix(".proxrs-")
        .tempfile_in(&directory)
        .context("could not stage the installed executable")?;
    io::copy(&mut source, &mut staged).context("could not copy the invoked executable")?;
    let mode = source.metadata()?.permissions().mode() & 0o777;
    staged
        .as_file()
        .set_permissions(fs::Permissions::from_mode(mode))
        .context("could not set installed executable permissions")?;
    staged
        .as_file()
        .sync_all()
        .context("could not finish writing the installed executable")?;
    let published = if replacing || options.force {
        staged.persist(&destination)
    } else {
        staged.persist_noclobber(&destination)
    };
    // PersistError owns the temporary file. Drop it here, before main exits,
    // rather than retaining it inside anyhow's error chain until process::exit.
    published
        .map_err(|error| error.error)
        .with_context(|| format!("could not install {:?}", destination))?;
    terminal::installation_complete(&destination, &path_status(&destination))?;
    Ok(0)
}

fn path_status(destination: &Path) -> PathStatus {
    let Some(path) = env::var_os("PATH") else {
        return PathStatus::Missing;
    };
    for directory in env::split_paths(&path) {
        let candidate = directory.join("proxrs");
        let Ok(metadata) = candidate.metadata() else {
            continue;
        };
        let Ok(name) = CString::new(candidate.as_os_str().as_bytes()) else {
            continue;
        };
        // access checks the caller's permissions, unlike testing whether any
        // owner/group/other execute bit happens to be set on the file.
        if metadata.is_file() && unsafe { libc::access(name.as_ptr(), libc::X_OK) } == 0 {
            return if candidate.canonicalize().ok().as_deref() == Some(destination) {
                PathStatus::Ready
            } else {
                PathStatus::Shadowed(candidate)
            };
        }
    }
    PathStatus::Missing
}
