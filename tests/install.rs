use std::{
    fs::{self, File},
    io::Write,
    os::{
        fd::FromRawFd,
        unix::{
            ffi::OsStringExt,
            fs::{MetadataExt, PermissionsExt, symlink},
        },
    },
    path::Path,
    process::{Child, Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

fn install(home: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_proxrs"));
    command
        .arg("install")
        .env("HOME", home)
        .env("PATH", "")
        .env("SHELL", "/bin/zsh");
    command
}

fn successful(output: &Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn installed_version(binary: &Path) {
    let output = Command::new(binary).arg("--version").output().unwrap();
    successful(&output);
    assert!(output.stdout.starts_with(b"proxrs "));
}

fn finish(mut child: Child) -> Output {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if child.try_wait().unwrap().is_some() {
            return child.wait_with_output().unwrap();
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            let output = child.wait_with_output().unwrap();
            panic!(
                "install timed out: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn with_terminal(command: &mut Command, answer: &[u8]) -> Output {
    let (mut master, mut slave) = (-1, -1);
    // Each test owns both descriptors of an otherwise unrelated pseudo-terminal.
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        0
    );
    let mut master = unsafe { File::from_raw_fd(master) };
    let slave = unsafe { File::from_raw_fd(slave) };
    let child = command
        .stdin(slave)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    master.write_all(answer).unwrap();
    finish(child)
}

#[test]
fn default_install_survives_removing_its_source_and_preserves_profiles() {
    let home = tempfile::tempdir().unwrap();
    let profiles = [
        ".profile",
        ".bashrc",
        ".bash_profile",
        ".zshrc",
        ".config/fish/config.fish",
    ];
    for profile in profiles {
        let path = home.path().join(profile);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "# existing configuration\n").unwrap();
    }
    let source = home.path().join("build/proxrs");
    fs::create_dir(source.parent().unwrap()).unwrap();
    fs::copy(env!("CARGO_BIN_EXE_proxrs"), &source).unwrap();
    let output = Command::new(&source)
        .arg("install")
        .env("HOME", home.path())
        .env("PATH", "")
        .env("SHELL", "/bin/zsh")
        .output()
        .unwrap();
    successful(&output);
    let destination = home.path().join(".local/bin/proxrs");
    assert_eq!(
        source.metadata().unwrap().len(),
        destination.metadata().unwrap().len()
    );
    assert_ne!(
        source.metadata().unwrap().ino(),
        destination.metadata().unwrap().ino()
    );
    fs::remove_file(source).unwrap();
    installed_version(&destination);
    for profile in profiles {
        assert_eq!(
            fs::read(home.path().join(profile)).unwrap(),
            b"# existing configuration\n"
        );
    }
    assert_eq!(
        fs::read_dir(destination.parent().unwrap()).unwrap().count(),
        1
    );
}

#[test]
fn custom_directory_works_without_home_and_reports_ready_path() {
    let temporary = tempfile::tempdir().unwrap();
    let directory = temporary.path().join("custom bin");
    let output = install(temporary.path())
        .args(["--bin-dir"])
        .arg(&directory)
        .env_remove("HOME")
        .env("PATH", &directory)
        .output()
        .unwrap();
    successful(&output);
    installed_version(&directory.join("proxrs"));
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8_lossy(&output.stderr).contains("available on PATH"));
    assert!(!temporary.path().join(".local").exists());
}

#[test]
fn noninteractive_replacement_requires_force_and_does_not_follow_symlinks() {
    let home = tempfile::tempdir().unwrap();
    let directory = home.path().join(".local/bin");
    fs::create_dir_all(&directory).unwrap();
    let unrelated = home.path().join("unrelated");
    fs::write(&unrelated, b"preserve this file").unwrap();
    let destination = directory.join("proxrs");
    symlink(&unrelated, &destination).unwrap();
    let output = install(home.path()).output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("--force"));
    assert!(output.stdout.is_empty());
    assert!(destination.is_symlink());
    successful(&install(home.path()).arg("--force").output().unwrap());
    assert!(!destination.is_symlink());
    assert_eq!(fs::read(&unrelated).unwrap(), b"preserve this file");
    installed_version(&destination);
}

#[test]
fn identical_binaries_still_prompt_and_empty_or_no_answers_preserve_them() {
    let home = tempfile::tempdir().unwrap();
    let directory = home.path().join(".local/bin");
    fs::create_dir_all(&directory).unwrap();
    let destination = directory.join("proxrs");
    fs::copy(env!("CARGO_BIN_EXE_proxrs"), &destination).unwrap();
    for answer in [b"\n".as_slice(), b"no\n", b"yes\n"] {
        let before = destination.metadata().unwrap().ino();
        let output = with_terminal(&mut install(home.path()), answer);
        successful(&output);
        assert!(String::from_utf8_lossy(&output.stderr).contains("[y/N]"));
        if answer == b"yes\n" {
            assert_ne!(before, destination.metadata().unwrap().ino());
        } else {
            assert_eq!(before, destination.metadata().unwrap().ino());
            assert!(output.stdout.is_empty());
            assert!(String::from_utf8_lossy(&output.stderr).contains("left unchanged"));
        }
    }
    installed_version(&destination);
    // Replacing the very executable currently running also keeps it usable.
    let output = Command::new(&destination)
        .args(["install", "--force"])
        .env("HOME", home.path())
        .env("PATH", &directory)
        .output()
        .unwrap();
    successful(&output);
    installed_version(&destination);
}

#[test]
fn path_setup_commands_handle_quoting_and_shadowed_commands() {
    let home = tempfile::tempdir().unwrap();
    let directory = home.path().join("space ' quote \\ $() ; bin");
    let competitor = home.path().join("competitor");
    fs::create_dir(&competitor).unwrap();
    fs::write(competitor.join("proxrs"), b"#!/bin/sh\nexit 99\n").unwrap();
    fs::set_permissions(competitor.join("proxrs"), fs::Permissions::from_mode(0o755)).unwrap();
    let path = std::env::join_paths([&competitor, &directory]).unwrap();
    for shell in ["/bin/sh", "/bin/bash", "/bin/zsh"] {
        let output = install(home.path())
            .arg("--bin-dir")
            .arg(&directory)
            .arg("--force")
            .env("PATH", &path)
            .env("SHELL", shell)
            .output()
            .unwrap();
        successful(&output);
        assert!(String::from_utf8_lossy(&output.stderr).contains("takes precedence"));
        let mut script = output.stdout;
        script.extend_from_slice(b"proxrs --version\n");
        let selected = Command::new(shell)
            .args(["-f", "-c"])
            .arg(std::ffi::OsString::from_vec(script))
            .env("HOME", home.path())
            .env("PATH", &path)
            .env_remove("BASH_ENV")
            .env_remove("ENV")
            .env_remove("ZDOTDIR")
            .output()
            .unwrap();
        successful(&selected);
        assert!(selected.stdout.starts_with(b"proxrs "));
    }
    if unsafe { libc::geteuid() } != 0 {
        // The owner cannot use a group-only execute bit, even if in that group.
        fs::set_permissions(competitor.join("proxrs"), fs::Permissions::from_mode(0o010)).unwrap();
        let output = install(home.path())
            .arg("--bin-dir")
            .arg(&directory)
            .arg("--force")
            .env("PATH", &path)
            .output()
            .unwrap();
        successful(&output);
        assert!(output.stdout.is_empty());
        assert!(String::from_utf8_lossy(&output.stderr).contains("available on PATH"));
    }
}

#[test]
fn fish_setup_is_session_only_and_quotes_the_directory() {
    let home = tempfile::tempdir().unwrap();
    let directory = home.path().join("fish ' quote \\ $() ; bin");
    let output = install(home.path())
        .arg("--bin-dir")
        .arg(&directory)
        .env("SHELL", "/usr/local/bin/fish")
        .output()
        .unwrap();
    successful(&output);
    assert!(
        output
            .stdout
            .starts_with(b"fish_add_path --path --prepend --move -- ")
    );
    // Fish is optional for developers. When present, execute the emitted command
    // as well as checking the CLI contract; no config or universal variables change.
    let fish = std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path)
            .map(|directory| directory.join("fish"))
            .find(|candidate| candidate.is_file())
    });
    if let Some(fish) = fish {
        let mut script = output.stdout;
        script.extend_from_slice(b"proxrs --version\n");
        let selected = Command::new(fish)
            .args(["--no-config", "-c"])
            .arg(std::ffi::OsString::from_vec(script))
            .env("HOME", home.path())
            .env("PATH", "")
            .env("XDG_CONFIG_HOME", home.path().join(".config"))
            .env("XDG_DATA_HOME", home.path().join(".data"))
            .output()
            .unwrap();
        successful(&selected);
        assert!(selected.stdout.starts_with(b"proxrs "));
        assert!(!home.path().join(".config/fish/fish_variables").exists());
    }
}

#[test]
fn unsupported_or_missing_shell_does_not_guess_setup_syntax() {
    let home = tempfile::tempdir().unwrap();
    for shell in [Some("/bin/unknown-shell"), None] {
        let mut command = install(home.path());
        command.arg("--force");
        if let Some(shell) = shell {
            command.env("SHELL", shell);
        } else {
            command.env_remove("SHELL");
        }
        let output = command.output().unwrap();
        successful(&output);
        assert!(output.stdout.is_empty());
        assert!(String::from_utf8_lossy(&output.stderr).contains("unavailable or unsupported"));
    }
}

#[test]
fn invalid_install_locations_fail_without_partial_files() {
    let home = tempfile::tempdir().unwrap();
    let output = install(home.path()).env_remove("HOME").output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("--bin-dir"));
    let invalid = home.path().join("colon:bin");
    let output = install(home.path())
        .arg("--bin-dir")
        .arg(&invalid)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(!invalid.exists());
    let directory = home.path().join(".local/bin/proxrs");
    fs::create_dir_all(&directory).unwrap();
    fs::write(directory.join("keep"), b"preserve").unwrap();
    let output = install(home.path()).arg("--force").output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("is a directory"));
    assert_eq!(fs::read(directory.join("keep")).unwrap(), b"preserve");
    assert_eq!(
        fs::read_dir(directory.parent().unwrap()).unwrap().count(),
        1
    );
}

#[test]
fn staging_failure_preserves_the_existing_executable() {
    // Root bypasses these permissions; the supported macOS baseline runs as a user.
    if unsafe { libc::geteuid() } == 0 {
        return;
    }
    let home = tempfile::tempdir().unwrap();
    let directory = home.path().join(".local/bin");
    fs::create_dir_all(&directory).unwrap();
    let destination = directory.join("proxrs");
    fs::write(&destination, b"keep the existing executable").unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o555)).unwrap();
    let output = install(home.path()).arg("--force").output();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
    let output = output.unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        fs::read(destination).unwrap(),
        b"keep the existing executable"
    );
    assert_eq!(fs::read_dir(directory).unwrap().count(), 1);
}
