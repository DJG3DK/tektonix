//! Docker Desktop: is it there, is it running, and installing it when not.
//!
//! The install path is Windows only and deliberately plain: Docker's own
//! installer, from Docker's own URL, run with its documented silent flags
//! and an elevation prompt the user sees. WSL 2 first when it is missing,
//! because Docker Desktop's installer assumes it. Virtualization switched
//! off in the firmware is the one thing nothing here can fix, so it is
//! named, with the remedy, instead of guessed at.

use crate::proc;
use serde::Serialize;
use tauri::{AppHandle, Emitter};

#[derive(Serialize, Clone, Copy, PartialEq, Eq, Debug)]
#[serde(rename_all = "kebab-case")]
pub enum DockerState {
    Missing,
    Stopped,
    NoCompose,
    Ready,
}

pub async fn state() -> DockerState {
    match proc::capture("docker", &["--version"], None).await {
        Err(proc::ProcError::Missing(_)) => return DockerState::Missing,
        Err(_) => return DockerState::Missing,
        Ok(_) => {}
    }
    if !proc::succeeds("docker", &["info"]).await {
        return DockerState::Stopped;
    }
    if !proc::succeeds("docker", &["compose", "version"]).await {
        return DockerState::NoCompose;
    }
    DockerState::Ready
}

fn note(app: &AppHandle, line: impl Into<String>) {
    let _ = app.emit(
        proc::LOG_EVENT,
        proc::LogLine {
            stream: "setup".into(),
            line: line.into(),
        },
    );
}

/// Set when this app started Docker Desktop, so Quit stops it again
/// (quit.rs). Docker Desktop the person started, or that starts with
/// Windows, is theirs and is left running.
#[derive(Default)]
pub struct StartedHere(pub std::sync::atomic::AtomicBool);

/// Stop Docker Desktop: `docker desktop stop`, Docker Desktop 4.37 and
/// later. An older one has no such command, and stays running.
pub async fn stop_desktop(app: &AppHandle) {
    note(app, "Stopping Docker Desktop...");
    if !proc::succeeds("docker", &["desktop", "stop"]).await {
        note(app, "Docker Desktop did not stop (it needs version 4.37 or later); quit it from its own tray icon.");
    }
}

/// Start Docker Desktop and wait for the daemon, up to two minutes.
pub async fn start_and_wait(app: &AppHandle) -> Result<(), String> {
    #[cfg(windows)]
    {
        let exe = ["C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe",
                   "C:\\Program Files (x86)\\Docker\\Docker\\Docker Desktop.exe"]
            .into_iter()
            .find(|p| std::path::Path::new(p).exists())
            .ok_or("Docker Desktop is installed but its program was not found; start it yourself and try again")?;
        note(app, "Starting Docker Desktop...");
        if let Some(flag) = tauri::Manager::try_state::<StartedHere>(app) {
            flag.0.store(true, std::sync::atomic::Ordering::SeqCst);
        }
        proc::capture(
            "powershell",
            &[
                "-NoProfile",
                "-Command",
                &format!("Start-Process -FilePath {}", ps_quote(exe)),
            ],
            None,
        )
        .await
        .map_err(|e| e.to_string())?;
    }
    #[cfg(not(windows))]
    {
        note(
            app,
            "Docker is not running. Start Docker Desktop (or the docker service) and try again.",
        );
        return Err("Docker is not running".into());
    }
    #[allow(unreachable_code)]
    for i in 0..60 {
        if proc::succeeds("docker", &["info"]).await {
            note(app, "Docker is running.");
            return Ok(());
        }
        if i % 10 == 0 {
            note(app, "Waiting for Docker to come up...");
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    Err("Docker Desktop did not come up within two minutes. Open it and look at what it says; \
        \"Virtualization support not detected\" means the CPU's virtualization is off in the firmware."
        .into())
}

#[cfg(windows)]
const DOCKER_INSTALLER_URL: &str =
    "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe";

/// A PowerShell single-quoted literal: the one character that needs
/// anything is the quote itself, doubled. A path holding an apostrophe
/// (a user called O'Brien) ended the literal early.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn ps_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

/// The script that runs Docker's installer elevated. Before the elevation
/// prompt it checks what was downloaded: a valid Authenticode signature,
/// and Docker's own name on it; an installer that fails that is not run.
/// The installer's exit code is the script's (`-PassThru`; `-Wait` alone
/// threw it away, and a failed install read as done).
#[cfg_attr(not(windows), allow(dead_code))]
pub fn installer_script(path: &str) -> String {
    let p = ps_quote(path);
    format!(
        "$sig = Get-AuthenticodeSignature -FilePath {p}; \
         if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*Docker Inc*') {{ \
           [Console]::Error.WriteLine('the downloaded installer is not signed by Docker Inc (' + $sig.Status + '); not running it'); exit 3 }}; \
         $p = Start-Process -FilePath {p} -ArgumentList 'install','--accept-license','--quiet' -Verb RunAs -PassThru -Wait; \
         exit $p.ExitCode"
    )
}

/// Install what is missing: WSL 2, then Docker Desktop. Each step elevates
/// through Windows' own prompt. Returns a note on what the user must do
/// next (a reboot, typically) or an error in plain words.
#[cfg(windows)]
pub async fn install(app: &AppHandle) -> Result<String, String> {
    let mut next = String::new();
    if !proc::succeeds("wsl", &["--status"]).await {
        note(
            app,
            "WSL 2 is not set up. Installing it (Windows will ask for permission)...",
        );
        let r = proc::capture("powershell", &["-NoProfile", "-Command",
            "Start-Process -FilePath wsl -ArgumentList '--install','--no-distribution' -Verb RunAs -Wait"], None).await;
        match r {
            Ok(_) => {
                next.push_str(
                    "WSL 2 was installed. Windows needs a restart before Docker can use it. ",
                );
            }
            Err(e) => return Err(format!("could not install WSL 2: {e}")),
        }
    }
    if state().await == DockerState::Missing {
        let dir = std::env::temp_dir();
        let path = dir.join("Docker Desktop Installer.exe");
        note(
            app,
            "Downloading Docker Desktop from desktop.docker.com (about 600 MB)...",
        );
        download(app, DOCKER_INSTALLER_URL, &path).await?;
        note(
            app,
            "Running the Docker Desktop installer (Windows will ask for permission)...",
        );
        let ps = installer_script(&path.display().to_string());
        proc::capture("powershell", &["-NoProfile", "-Command", &ps], None)
            .await
            .map_err(|e| format!("the Docker Desktop installer did not finish: {e}"))?;
        next.push_str("Docker Desktop is installed. Sign out and back in (or restart), open Docker Desktop once, then come back here.");
    }
    if next.is_empty() {
        next.push_str("Nothing to install.");
    }
    Ok(next)
}

#[cfg(not(windows))]
pub async fn install(_app: &AppHandle) -> Result<String, String> {
    Err(
        "Install Docker Desktop (macOS) or Docker Engine (Linux) yourself, then come back here."
            .into(),
    )
}

#[cfg(windows)]
async fn download(app: &AppHandle, url: &str, path: &std::path::Path) -> Result<(), String> {
    use tokio::io::AsyncWriteExt;
    let resp = reqwest::get(url)
        .await
        .map_err(|e| format!("download failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("download failed: HTTP {}", resp.status()));
    }
    let total = resp.content_length().unwrap_or(0);
    let mut file = tokio::fs::File::create(path)
        .await
        .map_err(|e| format!("could not write {}: {e}", path.display()))?;
    let mut got: u64 = 0;
    let mut last_pct = 0;
    let mut stream = resp.bytes_stream();
    use futures_util::StreamExt;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("download failed: {e}"))?;
        file.write_all(&chunk).await.map_err(|e| e.to_string())?;
        got += chunk.len() as u64;
        if total > 0 {
            let pct = (got * 100 / total) as u32;
            if pct >= last_pct + 10 {
                last_pct = pct;
                note(app, format!("Downloaded {pct}%"));
            }
        }
    }
    file.flush().await.map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_path_with_an_apostrophe_survives_powershell() {
        assert_eq!(
            ps_quote("C:\\Users\\O'Brien\\Docker Desktop Installer.exe"),
            "'C:\\Users\\O''Brien\\Docker Desktop Installer.exe'"
        );
        assert_eq!(ps_quote("plain"), "'plain'");
    }

    #[test]
    fn the_installer_is_checked_before_elevation_and_its_exit_code_is_kept() {
        let script = installer_script("C:\\Temp\\Docker Desktop Installer.exe");
        let check = script
            .find("Get-AuthenticodeSignature")
            .expect("signature check");
        let run = script.find("Start-Process").expect("the run");
        assert!(check < run, "the check comes before the elevation prompt");
        assert!(
            script.contains("-notlike '*Docker Inc*'"),
            "Docker's own name on the signature"
        );
        assert!(
            script.contains("-PassThru -Wait"),
            "-Wait alone threw the exit code away"
        );
        assert!(script.ends_with("exit $p.ExitCode"));
        assert!(script.contains("-FilePath 'C:\\Temp\\Docker Desktop Installer.exe'"));
    }
}
