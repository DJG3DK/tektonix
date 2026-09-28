//! The stack under the app: the compose files it ships, the .env it keeps,
//! the released images it pulls, and `docker compose` around them.
//!
//! Layout, under the app's local data directory (`%LOCALAPPDATA%\Tektonix`
//! on Windows):
//!
//!   stack/docker-compose.yml        copied from the app's resources on every
//!   stack/docker/...                launch, so an app update carries the
//!   stack/services/model-router/... stack's own files with it
//!   stack/.env                      the operator's: written by setup, never
//!                                   overwritten
//!   stack/version.json              which release's images are installed
//!
//! Images come from GHCR as ghcr.io/djg3dk/tektonix-<name>:<tag> and are
//! tagged with the local name the compose file uses (tektonix-<name>:latest),
//! so `docker compose up -d --no-build` starts a release the same way a
//! source checkout's `--build` would.

use crate::proc;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::{AppHandle, Emitter, Manager};

pub const IMAGES: [&str; 4] = ["agent", "router", "reviewer", "sandbox"];
pub const REGISTRY: &str = "ghcr.io/djg3dk";
pub const REPO: &str = "DJG3DK/tektonix";
pub const DASHBOARD: &str = "http://localhost:8100";
const HEALTH: &str = "http://127.0.0.1:8100/api/health";

/// The stack's files the app carries, relative to both the resource dir's
/// `stack/` and the data dir's `stack/`.
const SHIPPED: [&str; 5] = [
    "docker-compose.yml",
    ".env.example",
    "docker/checks-postgres/init.sh",
    "docker/postgres/init-secrets.sh",
    "services/model-router/config.example.yaml",
];

#[derive(Serialize, Deserialize, Clone, Default)]
pub struct Settings {
    pub openrouter_api_key_set: bool,
    pub openrouter_key_hint: String,
    pub projects_dir: String,
    pub admin_email: String,
    /// Whose commits these are; blank falls back to "Tektonix" and the
    /// sign-in address (docker-compose.yml).
    pub git_name: String,
    pub git_email: String,
}

#[derive(Serialize, Clone)]
pub struct Container {
    pub name: String,
    pub service: String,
    pub state: String,
    pub health: String,
}

#[derive(Serialize, Deserialize, Clone, Default)]
pub struct Version {
    pub tag: String,
}

#[derive(Serialize, Clone)]
pub struct UpdateInfo {
    pub installed: String,
    pub latest: String,
    pub available: bool,
    pub url: String,
    pub notes: String,
}

pub fn dir(app: &AppHandle) -> Result<PathBuf, String> {
    let base = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    Ok(base.join("stack"))
}

fn resources(app: &AppHandle) -> Result<PathBuf, String> {
    let base = app.path().resource_dir().map_err(|e| e.to_string())?;
    Ok(base.join("stack"))
}

fn note(app: &AppHandle, line: impl Into<String>) {
    let _ = app.emit(proc::LOG_EVENT, proc::LogLine { stream: "app".into(), line: line.into() });
}

/// Copy the shipped stack files into the data directory. Every launch, so
/// an app update updates the compose file with it; never .env.
pub fn prepare(app: &AppHandle) -> Result<PathBuf, String> {
    let src = resources(app)?;
    let dst = dir(app)?;
    for rel in SHIPPED {
        let from = src.join(rel);
        let to = dst.join(rel);
        if let Some(parent) = to.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("could not create {}: {e}", parent.display()))?;
        }
        std::fs::copy(&from, &to).map_err(|e| format!("could not copy {}: {e}", from.display()))?;
    }
    // The compose file binds this directory into the agent; an absent source
    // becomes an empty directory docker creates as root. Make it ourselves.
    std::fs::create_dir_all(dst.join("docker/agent-sandbox")).map_err(|e| e.to_string())?;
    let env = dst.join(".env");
    if !env.exists() {
        std::fs::copy(dst.join(".env.example"), &env).map_err(|e| e.to_string())?;
    }
    Ok(dst)
}

// ── .env ─────────────────────────────────────────────────────────────────────

/// Replace `KEY=` in place, or append it. A commented-out line is left as
/// documentation. Same rules as install.ps1's Set-EnvLine.
pub fn set_env_line(content: &str, key: &str, value: &str) -> String {
    let mut out = Vec::new();
    let mut done = false;
    for line in content.lines() {
        let trimmed = line.trim_start();
        if !done && trimmed.starts_with(key) && trimmed[key.len()..].trim_start().starts_with('=') && !trimmed.starts_with('#') {
            out.push(format!("{key}={value}"));
            done = true;
        } else {
            out.push(line.to_string());
        }
    }
    if !done {
        out.push(format!("{key}={value}"));
    }
    let mut s = out.join("\n");
    s.push('\n');
    s
}

pub fn get_env_value(content: &str, key: &str) -> Option<String> {
    content.lines().find_map(|line| {
        let t = line.trim();
        if t.starts_with('#') {
            return None;
        }
        let (k, v) = t.split_once('=')?;
        if k.trim() != key {
            return None;
        }
        let v = v.trim().trim_matches('"').trim_matches('\'').to_string();
        Some(v)
    })
}

fn env_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(dir(app)?.join(".env"))
}

pub fn read_settings(app: &AppHandle) -> Result<Settings, String> {
    let content = std::fs::read_to_string(env_path(app)?).unwrap_or_default();
    let key = get_env_value(&content, "OPENROUTER_API_KEY").unwrap_or_default();
    Ok(Settings {
        openrouter_api_key_set: !key.is_empty(),
        openrouter_key_hint: hint(&key),
        projects_dir: get_env_value(&content, "PROJECTS_DIR").unwrap_or_default(),
        admin_email: get_env_value(&content, "ADMIN_EMAIL").unwrap_or_else(|| "admin@example.com".into()),
        git_name: get_env_value(&content, "GIT_USER_NAME").unwrap_or_default(),
        git_email: get_env_value(&content, "GIT_USER_EMAIL").unwrap_or_default(),
    })
}

/// This machine's own git identity, to prefill the form: most people who
/// have git have set it once.
pub async fn machine_git_identity() -> (String, String) {
    let name = proc::capture("git", &["config", "--global", "user.name"], None).await.unwrap_or_default();
    let email = proc::capture("git", &["config", "--global", "user.email"], None).await.unwrap_or_default();
    (name.trim().to_string(), email.trim().to_string())
}

fn hint(secret: &str) -> String {
    if secret.len() <= 8 {
        return "•".repeat(secret.len());
    }
    format!("••••••••{}", &secret[secret.len() - 4..])
}

pub fn save_settings(app: &AppHandle, key: Option<String>, projects_dir: String, admin_email: String,
                     git_name: String, git_email: String) -> Result<Settings, String> {
    let projects_dir = projects_dir.trim().trim_matches('"').trim_end_matches(['\\', '/']).to_string();
    if projects_dir.is_empty() || !Path::new(&projects_dir).is_absolute() {
        return Err("the projects folder must be a full path, for example C:\\Users\\you\\code".into());
    }
    std::fs::create_dir_all(&projects_dir).map_err(|e| format!("could not create {projects_dir}: {e}"))?;
    let path = env_path(app)?;
    let mut content = std::fs::read_to_string(&path).unwrap_or_default();
    if let Some(k) = key.map(|k| k.trim().to_string()).filter(|k| !k.is_empty()) {
        content = set_env_line(&content, "OPENROUTER_API_KEY", &k);
    }
    content = set_env_line(&content, "PROJECTS_DIR", &projects_dir);
    let email = admin_email.trim();
    if !email.is_empty() {
        content = set_env_line(&content, "ADMIN_EMAIL", email);
    }
    content = set_env_line(&content, "GIT_USER_NAME", git_name.trim());
    content = set_env_line(&content, "GIT_USER_EMAIL", git_email.trim());
    std::fs::write(&path, content).map_err(|e| e.to_string())?;
    read_settings(app)
}

// ── versions and images ─────────────────────────────────────────────────────

fn version_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(dir(app)?.join("version.json"))
}

pub fn installed_version(app: &AppHandle) -> Option<String> {
    let text = std::fs::read_to_string(version_path(app).ok()?).ok()?;
    serde_json::from_str::<Version>(&text).ok().map(|v| v.tag).filter(|t| !t.is_empty())
}

fn write_version(app: &AppHandle, tag: &str) -> Result<(), String> {
    let text = serde_json::to_string(&Version { tag: tag.into() }).map_err(|e| e.to_string())?;
    std::fs::write(version_path(app)?, text).map_err(|e| e.to_string())
}

pub fn image_ref(name: &str, tag: &str) -> String {
    format!("{REGISTRY}/tektonix-{name}:{tag}")
}

pub fn local_name(name: &str) -> String {
    format!("tektonix-{name}:latest")
}

/// Pull one release's images and tag them with the local names. When the
/// versioned tag does not exist (an app built ahead of its release, or a
/// pre-release build), the registry's `latest` is pulled instead and the
/// installed version is recorded as that.
pub async fn pull(app: &AppHandle, tag: &str) -> Result<(), String> {
    let mut used = tag.to_string();
    for name in IMAGES {
        let mut remote = image_ref(name, &used);
        note(app, format!("Pulling {remote}"));
        let mut code = proc::stream(app, "docker", "docker", &["pull", &remote], None).await.map_err(|e| e.to_string())?;
        if code != 0 && used != "latest" {
            note(app, format!("No {used} image for {name}; using the latest published one."));
            used = "latest".into();
            remote = image_ref(name, &used);
            code = proc::stream(app, "docker", "docker", &["pull", &remote], None).await.map_err(|e| e.to_string())?;
        }
        if code != 0 {
            return Err(format!("could not pull {remote} (exit {code}). Is this machine online, and does the release exist?"));
        }
        proc::capture("docker", &["tag", &remote, &local_name(name)], None).await.map_err(|e| e.to_string())?;
    }
    write_version(app, &used)?;
    Ok(())
}

fn compose_args<'a>(dir: &'a Path, rest: &[&'a str]) -> Vec<String> {
    let mut v = vec!["compose".to_string(), "--project-directory".into(), dir.display().to_string(),
                     "-f".into(), dir.join("docker-compose.yml").display().to_string()];
    v.extend(rest.iter().map(|s| s.to_string()));
    v
}

async fn compose_stream(app: &AppHandle, rest: &[&str]) -> Result<i32, String> {
    let dir = dir(app)?;
    let args = compose_args(&dir, rest);
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    proc::stream(app, "compose", "docker", &refs, Some(&dir)).await.map_err(|e| e.to_string())
}

async fn compose_capture(app: &AppHandle, rest: &[&str]) -> Result<String, String> {
    let dir = dir(app)?;
    let args = compose_args(&dir, rest);
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    proc::capture("docker", &refs, Some(&dir)).await.map_err(|e| e.to_string())
}

/// `docker compose up -d --no-build`, then wait for the dashboard.
pub async fn up(app: &AppHandle) -> Result<(), String> {
    let settings = read_settings(app)?;
    if !settings.openrouter_api_key_set || settings.projects_dir.is_empty() {
        return Err("set the OpenRouter key and the projects folder first".into());
    }
    note(app, "Starting the stack...");
    let code = compose_stream(app, &["up", "-d", "--no-build", "--remove-orphans"]).await?;
    if code != 0 {
        return Err(format!("docker compose up failed (exit {code}); the lines above are Docker's own"));
    }
    wait_healthy(app).await
}

async fn wait_healthy(app: &AppHandle) -> Result<(), String> {
    let client = reqwest::Client::builder().timeout(std::time::Duration::from_secs(3)).build().map_err(|e| e.to_string())?;
    for i in 0..150 {
        if let Ok(r) = client.get(HEALTH).send().await {
            if r.status().is_success() {
                note(app, format!("Tektonix is running at {DASHBOARD}"));
                return Ok(());
            }
        }
        if i % 15 == 0 {
            note(app, "Waiting for the agent to answer...");
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    Err("the agent did not answer within five minutes; see the agent log".into())
}

pub async fn down(app: &AppHandle) -> Result<(), String> {
    note(app, "Stopping the stack...");
    let code = compose_stream(app, &["stop"]).await?;
    if code != 0 {
        return Err(format!("docker compose stop failed (exit {code})"));
    }
    note(app, "Stopped.");
    Ok(())
}

pub async fn status(app: &AppHandle) -> Result<Vec<Container>, String> {
    let out = compose_capture(app, &["ps", "-a", "--format", "json"]).await.unwrap_or_default();
    // compose prints one JSON object per line (older versions: an array).
    let mut rows = Vec::new();
    let mut objects: Vec<serde_json::Value> = Vec::new();
    if let Ok(serde_json::Value::Array(arr)) = serde_json::from_str::<serde_json::Value>(&out) {
        objects = arr;
    } else {
        for line in out.lines() {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                objects.push(v);
            }
        }
    }
    for v in objects {
        let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
        rows.push(Container { name: s("Name"), service: s("Service"), state: s("State"), health: s("Health") });
    }
    Ok(rows)
}


pub async fn initial_password(app: &AppHandle) -> Result<String, String> {
    let out = compose_capture(app, &["exec", "-T", "agent", "python", "scripts/show_initial_password.py"]).await?;
    Ok(out.trim().to_string())
}

// ── updates ──────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct Release {
    pub tag_name: String,
    pub html_url: String,
    #[serde(default)]
    pub body: String,
}

pub async fn latest_release() -> Result<Release, String> {
    let client = reqwest::Client::builder().user_agent("tektonix-desktop").build().map_err(|e| e.to_string())?;
    let url = format!("https://api.github.com/repos/{REPO}/releases/latest");
    let r = client.get(&url).send().await.map_err(|e| format!("could not reach GitHub: {e}"))?;
    if !r.status().is_success() {
        return Err(format!("GitHub answered {} for the latest release", r.status()));
    }
    r.json::<Release>().await.map_err(|e| format!("unexpected answer from GitHub: {e}"))
}

pub async fn check_update(app: &AppHandle) -> Result<UpdateInfo, String> {
    let latest = latest_release().await?;
    let installed = installed_version(app).unwrap_or_default();
    Ok(UpdateInfo {
        available: !installed.is_empty() && installed != latest.tag_name,
        installed,
        latest: latest.tag_name,
        url: latest.html_url,
        notes: latest.body,
    })
}

/// Pull the release's images and restart the stack on them.
pub async fn update_to(app: &AppHandle, tag: &str) -> Result<(), String> {
    pull(app, tag).await?;
    up(app).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn set_env_line_replaces_in_place_or_appends() {
        let s = "A=1\n# PROJECTS_DIR=old\nPROJECTS_DIR=\nB=2\n";
        let out = set_env_line(s, "PROJECTS_DIR", "C:\\Users\\me\\code");
        assert_eq!(out, "A=1\n# PROJECTS_DIR=old\nPROJECTS_DIR=C:\\Users\\me\\code\nB=2\n");
        let out = set_env_line("A=1", "NEW", "x");
        assert_eq!(out, "A=1\nNEW=x\n");
        let out = set_env_line("PROJECTS_DIR_OLD=1\n", "PROJECTS_DIR", "x");
        assert_eq!(out, "PROJECTS_DIR_OLD=1\nPROJECTS_DIR=x\n", "a different key that starts the same is left alone");
    }

    #[test]
    fn get_env_value_skips_comments_and_strips_quotes() {
        let s = "# OPENROUTER_API_KEY=nope\nOPENROUTER_API_KEY=\"sk-or-1234\"\nPROJECTS_DIR=C:\\code\n";
        assert_eq!(get_env_value(s, "OPENROUTER_API_KEY").as_deref(), Some("sk-or-1234"));
        assert_eq!(get_env_value(s, "PROJECTS_DIR").as_deref(), Some("C:\\code"));
        assert_eq!(get_env_value(s, "MISSING"), None);
    }

    #[test]
    fn image_names_follow_the_release_workflow_and_the_compose_file() {
        assert_eq!(image_ref("agent", "v0.9.0"), "ghcr.io/djg3dk/tektonix-agent:v0.9.0");
        assert_eq!(local_name("reviewer"), "tektonix-reviewer:latest");
        assert_eq!(IMAGES, ["agent", "router", "reviewer", "sandbox"]);
    }

    #[test]
    fn a_secret_is_hinted_never_shown() {
        assert_eq!(hint("sk-or-v1-abcdefgh1234"), "••••••••1234");
        assert_eq!(hint("short"), "•••••");
    }
}
