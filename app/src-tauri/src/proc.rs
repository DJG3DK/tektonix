//! Running programs: captured, or streamed line by line to the window.
//!
//! Everything the app does to the stack is a `docker` invocation, and the
//! operator needs to see Docker's own words while it runs: the first pull is
//! gigabytes, and a window that says "starting" for ten minutes with nothing
//! under it is what the console installer got wrong.

use serde::Serialize;
use std::path::Path;
use std::process::Stdio;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

#[derive(Debug, thiserror::Error)]
pub enum ProcError {
    #[error("{0} is not installed or not on PATH")]
    Missing(String),
    #[error("{program} failed (exit {code}): {tail}")]
    Failed { program: String, code: i32, tail: String },
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

/// One line of a running program, as the window shows it.
#[derive(Clone, Serialize)]
pub struct LogLine {
    pub stream: String,
    pub line: String,
}

pub const LOG_EVENT: &str = "stack-log";

fn command(program: &str, args: &[&str], cwd: Option<&Path>) -> Command {
    let mut cmd = Command::new(program);
    cmd.args(args);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    cmd.stdin(Stdio::null());
    #[cfg(windows)]
    {
        // No console window flashing up behind the app for every docker call.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

fn missing(program: &str, e: &std::io::Error) -> bool {
    let _ = program;
    e.kind() == std::io::ErrorKind::NotFound
}

/// Run and capture. Ok(stdout) on exit 0, Err with the last lines otherwise.
pub async fn capture(program: &str, args: &[&str], cwd: Option<&Path>) -> Result<String, ProcError> {
    let out = command(program, args, cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| if missing(program, &e) { ProcError::Missing(program.into()) } else { ProcError::Io(e) })?;
    if out.status.success() {
        return Ok(String::from_utf8_lossy(&out.stdout).into_owned());
    }
    let text = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    Err(ProcError::Failed {
        program: program.into(),
        code: out.status.code().unwrap_or(-1),
        tail: tail(&text, 12),
    })
}

/// Whether the program runs and exits 0. Missing counts as false.
pub async fn succeeds(program: &str, args: &[&str]) -> bool {
    capture(program, args, None).await.is_ok()
}

/// Run and stream every stdout and stderr line to the window under `label`,
/// returning the exit code. The caller decides what a non-zero exit means.
pub async fn stream(app: &AppHandle, label: &str, program: &str, args: &[&str], cwd: Option<&Path>) -> Result<i32, ProcError> {
    let mut child = command(program, args, cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| if missing(program, &e) { ProcError::Missing(program.into()) } else { ProcError::Io(e) })?;
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let (a, b) = (app.clone(), app.clone());
    let (la, lb) = (label.to_string(), label.to_string());
    let t1 = tokio::spawn(async move {
        if let Some(out) = stdout {
            let mut lines = BufReader::new(out).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = a.emit(LOG_EVENT, LogLine { stream: la.clone(), line });
            }
        }
    });
    let t2 = tokio::spawn(async move {
        if let Some(err) = stderr {
            let mut lines = BufReader::new(err).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = b.emit(LOG_EVENT, LogLine { stream: lb.clone(), line });
            }
        }
    });
    let status = child.wait().await?;
    let _ = t1.await;
    let _ = t2.await;
    Ok(status.code().unwrap_or(-1))
}

/// A streaming child the caller can stop: `docker compose logs -f`.
pub struct Streaming {
    child: tokio::process::Child,
}

impl Streaming {
    pub async fn stop(mut self) {
        let _ = self.child.kill().await;
    }
}

pub fn spawn_streaming(app: &AppHandle, label: &str, program: &str, args: &[&str], cwd: Option<&Path>) -> Result<Streaming, ProcError> {
    let mut child = command(program, args, cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| if missing(program, &e) { ProcError::Missing(program.into()) } else { ProcError::Io(e) })?;
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    for (pipe, which) in [(stdout.map(|s| Box::new(s) as Box<dyn tokio::io::AsyncRead + Unpin + Send>), "out"),
                          (stderr.map(|s| Box::new(s) as Box<dyn tokio::io::AsyncRead + Unpin + Send>), "err")] {
        let _ = which;
        if let Some(pipe) = pipe {
            let a = app.clone();
            let l = label.to_string();
            tokio::spawn(async move {
                let mut lines = BufReader::new(pipe).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    let _ = a.emit(LOG_EVENT, LogLine { stream: l.clone(), line });
                }
            });
        }
    }
    Ok(Streaming { child })
}

pub fn tail(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let start = lines.len().saturating_sub(n);
    lines[start..].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tail_keeps_the_last_lines() {
        assert_eq!(tail("a\nb\nc\nd", 2), "c\nd");
        assert_eq!(tail("a", 5), "a");
        assert_eq!(tail("", 3), "");
    }
}
