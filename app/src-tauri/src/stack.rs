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

/// What the stack runs: the release tag, and the id of the agent image
/// that tag was pulled as. The id is the proof; a record without one (an
/// early candidate wrote the requested tag whether or not the pull got it)
/// or whose image is no longer the local one is not trusted.
#[derive(Serialize, Deserialize, Clone, Default)]
pub struct Version {
    pub tag: String,
    #[serde(default)]
    pub image_id: Option<String>,
}

/// The app's own preferences, in the data directory beside the stack.
#[derive(Serialize, Deserialize, Clone)]
pub struct Prefs {
    /// Check on startup and every few hours; update the stack when a newer
    /// release is out and the agent is idle, then the app itself.
    pub auto_update: bool,
    /// Count pre-releases (a dash in the tag) as releases.
    pub include_prereleases: bool,
    /// "console" or "panel": what the window showed last, so a restart
    /// (an update's, most of all) comes back to it.
    #[serde(default)]
    pub last_page: String,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs { auto_update: true, include_prereleases: false, last_page: String::new() }
    }
}

/// Record what the window shows; nothing else in the preferences moves.
pub fn remember_page(app: &AppHandle, page: &str) {
    let mut prefs = read_prefs(app);
    if prefs.last_page != page {
        prefs.last_page = page.to_string();
        let _ = write_prefs(app, &prefs);
    }
}

/// A release-candidate app counts release candidates as releases whatever
/// the preference says: its next update IS one. 2026-09-29: with the box
/// unticked, an rc app compared itself to the last stable release and said
/// it was up to date, forever.
pub fn wants_prereleases(app: &AppHandle) -> bool {
    read_prefs(app).include_prereleases || release_tag(app).contains('-')
}

/// Whether someone is using the window right now: it is shown and has the
/// focus. An update never restarts the app under a person's hands.
pub fn window_in_use(app: &AppHandle) -> bool {
    use tauri::Manager;
    match app.get_webview_window("main") {
        Some(w) => w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false),
        None => false,
    }
}

fn prefs_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(dir(app)?.join("prefs.json"))
}

pub fn read_prefs(app: &AppHandle) -> Prefs {
    prefs_path(app).ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default()
}

pub fn write_prefs(app: &AppHandle, prefs: &Prefs) -> Result<Prefs, String> {
    let path = prefs_path(app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, serde_json::to_string_pretty(prefs).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    Ok(prefs.clone())
}

#[derive(Serialize, Clone)]
pub struct UpdateInfo {
    /// The stack's recorded release; empty when nothing was recorded.
    pub installed: String,
    /// Whether that record is proven by the local image (stack.rs Version).
    pub stack_verified: bool,
    /// This app's own release.
    pub app_version: String,
    /// The release the stack should be on: the newer of GitHub's latest
    /// and this app's own.
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
    // This is the desktop app: the single-user sign-in rules (agent/auth.py,
    // desktop_install). Only the app writes this line.
    let mut content = std::fs::read_to_string(&env).unwrap_or_default();
    if get_env_value(&content, "TEKTONIX_DESKTOP").as_deref() != Some("1") {
        content = set_env_line(&content, "TEKTONIX_DESKTOP", "1");
    }
    // A machine of its own: checks get half its cores and four gigabytes,
    // written once so the operator can change them in the file.
    if get_env_value(&content, "SANDBOX_CPUS").is_none() {
        content = set_env_line(&content, "SANDBOX_CPUS", &sandbox_cpus(machine_cores()).to_string());
    }
    if get_env_value(&content, "SANDBOX_MEMORY").is_none() {
        content = set_env_line(&content, "SANDBOX_MEMORY", "4g");
    }
    std::fs::write(&env, content).map_err(|e| e.to_string())?;
    Ok(dst)
}

fn machine_cores() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4)
}

/// Half the machine, never fewer than two, never more than eight.
pub fn sandbox_cpus(cores: usize) -> usize {
    (cores / 2).clamp(2, 8)
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

fn read_version(app: &AppHandle) -> Option<Version> {
    let text = std::fs::read_to_string(version_path(app).ok()?).ok()?;
    serde_json::from_str::<Version>(&text).ok().filter(|v| !v.tag.is_empty())
}

fn write_version(app: &AppHandle, tag: &str, image_id: Option<String>) -> Result<(), String> {
    let text = serde_json::to_string(&Version { tag: tag.into(), image_id }).map_err(|e| e.to_string())?;
    std::fs::write(version_path(app)?, text).map_err(|e| e.to_string())
}

/// The id of the agent image the compose file runs, as Docker has it now.
async fn local_agent_image_id() -> Option<String> {
    let out = proc::capture("docker", &["image", "inspect", &local_name("agent"), "--format", "{{.Id}}"], None).await.ok()?;
    let id = out.trim().to_string();
    (!id.is_empty()).then_some(id)
}

/// Does the record say what the local images are? True only when it names
/// an image id and that is the image Docker has under the local name.
fn record_is_proven(record: &Version, local_id: Option<&str>) -> bool {
    matches!((record.image_id.as_deref(), local_id), (Some(a), Some(b)) if a == b)
}

/// The release this app was built from: the tag the release workflow bakes
/// in, else the package version. 2026-09-29: an rc app asked for `v0.9.0`
/// images, no such release existed, and the pull fell back to `latest`, so
/// every release candidate ran the previous stable release's stack.
pub fn release_tag(app: &AppHandle) -> String {
    match option_env!("TEKTONIX_RELEASE_TAG").map(str::trim).filter(|t| !t.is_empty()) {
        Some(t) => t.to_string(),
        None => format!("v{}", app.package_info().version),
    }
}

/// `vMAJOR.MINOR.PATCH`, with an optional `-rcN`: a pre-release sorts below
/// the release it leads to. Anything unparseable sorts lowest.
fn version_key(tag: &str) -> (u64, u64, u64, bool, u64) {
    let raw = tag.trim().trim_start_matches('v');
    let (core, pre) = match raw.split_once('-') { Some((c, p)) => (c, Some(p)), None => (raw, None) };
    let mut nums = core.split('.').map(|n| n.parse::<u64>().unwrap_or(0));
    let (maj, min, pat) = (nums.next().unwrap_or(0), nums.next().unwrap_or(0), nums.next().unwrap_or(0));
    match pre {
        None => (maj, min, pat, true, 0),
        Some(p) => (maj, min, pat, false, p.trim_start_matches(|c: char| !c.is_ascii_digit()).parse().unwrap_or(0)),
    }
}

pub fn newer_than(candidate: &str, installed: &str) -> bool {
    version_key(candidate) > version_key(installed)
}

pub fn image_ref(name: &str, tag: &str) -> String {
    format!("{REGISTRY}/tektonix-{name}:{tag}")
}

pub fn local_name(name: &str) -> String {
    format!("tektonix-{name}:latest")
}

/// Pull one release's images and tag them with the local names. The images
/// are published before the installer (release.yml), so a release the app
/// knows always has them; a missing image is an error, never a quiet swap
/// for some other release's code.
pub async fn pull(app: &AppHandle, tag: &str) -> Result<(), String> {
    for name in IMAGES {
        let remote = image_ref(name, tag);
        note(app, format!("Pulling {remote}"));
        let code = proc::stream(app, "docker", "docker", &["pull", &remote], None).await.map_err(|e| e.to_string())?;
        if code != 0 {
            return Err(format!("could not pull {remote} (exit {code}). Is this machine online, and is {tag} a published release?"));
        }
        proc::capture("docker", &["tag", &remote, &local_name(name)], None).await.map_err(|e| e.to_string())?;
    }
    write_version(app, tag, local_agent_image_id().await)?;
    Ok(())
}

/// The stack this app runs is at least the release it was built from. A new
/// app over an older stack (or a stack recorded as `latest` by the old
/// fallback) pulls its own images before it starts; a stack the automatic
/// update already moved ahead of the app is left where it is.
/// Says whether it pulled, so the caller knows to restart the stack on it.
pub async fn ensure_own_release(app: &AppHandle) -> Result<bool, String> {
    let wanted = release_tag(app);
    let record = read_version(app);
    let local = local_agent_image_id().await;
    let proven = record.as_ref().is_some_and(|r| record_is_proven(r, local.as_deref()));
    if let Some(r) = &record {
        if proven && !newer_than(&wanted, &r.tag) {
            return Ok(false);
        }
    }
    // A running task is not interrupted for this: the automatic pass
    // comes back to it once the agent is idle.
    if agent_busy().await == Some(true) {
        note(app, format!("The agent is busy; the stack moves to {wanted} when it is idle."));
        return Ok(false);
    }
    note(app, match &record {
        Some(r) if proven => format!("This app is {wanted}; the stack is {}. Pulling {wanted}.", r.tag),
        Some(r) => format!("The stack record says {} but cannot be verified. Pulling {wanted}.", r.tag),
        None => format!("Pulling the {wanted} stack."),
    });
    pull(app, &wanted).await?;
    Ok(true)
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
    #[serde(default)]
    pub draft: bool,
}

pub async fn latest_release(include_prereleases: bool) -> Result<Release, String> {
    let client = reqwest::Client::builder().user_agent("tektonix-desktop").build().map_err(|e| e.to_string())?;
    if !include_prereleases {
        let url = format!("https://api.github.com/repos/{REPO}/releases/latest");
        let r = client.get(&url).send().await.map_err(|e| format!("could not reach GitHub: {e}"))?;
        if !r.status().is_success() {
            return Err(format!("GitHub answered {} for the latest release", r.status()));
        }
        return r.json::<Release>().await.map_err(|e| format!("unexpected answer from GitHub: {e}"));
    }
    // Newest first; drafts never count, pre-releases do.
    let url = format!("https://api.github.com/repos/{REPO}/releases?per_page=10");
    let r = client.get(&url).send().await.map_err(|e| format!("could not reach GitHub: {e}"))?;
    if !r.status().is_success() {
        return Err(format!("GitHub answered {} for the releases", r.status()));
    }
    let all = r.json::<Vec<Release>>().await.map_err(|e| format!("unexpected answer from GitHub: {e}"))?;
    newest_release(all).ok_or_else(|| "no releases yet".to_string())
}

/// The newest by version among the published ones. GitHub's listing is
/// not newest first: on 2026-09-29 it led with rc9 above rc14, and the app
/// announced rc9 as the release that was out.
pub fn newest_release(all: Vec<Release>) -> Option<Release> {
    all.into_iter().filter(|rel| !rel.draft).max_by_key(|rel| version_key(&rel.tag_name))
}

pub async fn check_update(app: &AppHandle) -> Result<UpdateInfo, String> {
    let latest = latest_release(wants_prereleases(app)).await?;
    let mine = release_tag(app);
    let record = read_version(app);
    let local = local_agent_image_id().await;
    let proven = record.as_ref().is_some_and(|r| record_is_proven(r, local.as_deref()));
    let installed = record.map(|r| r.tag).unwrap_or_default();
    // An app built ahead of GitHub's newest listing still wants its own.
    let target = if newer_than(&mine, &latest.tag_name) { mine.clone() } else { latest.tag_name.clone() };
    Ok(UpdateInfo {
        available: !proven || newer_than(&target, &installed),
        installed,
        stack_verified: proven,
        app_version: mine,
        latest: target,
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
    fn checks_get_half_the_machine_within_bounds() {
        assert_eq!(sandbox_cpus(2), 2);
        assert_eq!(sandbox_cpus(8), 4);
        assert_eq!(sandbox_cpus(32), 8);
    }

    #[test]
    fn the_newest_release_is_chosen_by_version_not_by_listing_order() {
        let rel = |tag: &str, draft: bool| Release { tag_name: tag.into(), html_url: String::new(), body: String::new(), draft };
        let all = vec![rel("v0.9.0-rc9", false), rel("v0.9.0-rc14", false), rel("v0.9.0-rc15", true), rel("v0.9.0-rc13", false)];
        assert_eq!(newest_release(all).map(|r| r.tag_name).as_deref(), Some("v0.9.0-rc14"), "a draft never counts");
        assert!(newest_release(vec![]).is_none());
    }

    #[test]
    fn a_record_is_trusted_only_with_the_image_it_names() {
        let legacy = Version { tag: "v0.9.0".into(), image_id: None };
        assert!(!record_is_proven(&legacy, Some("sha256:abc")), "an early candidate's record names no image");
        let mine = Version { tag: "v0.9.0-rc13".into(), image_id: Some("sha256:abc".into()) };
        assert!(record_is_proven(&mine, Some("sha256:abc")));
        assert!(!record_is_proven(&mine, Some("sha256:other")), "someone retagged the local image");
        assert!(!record_is_proven(&mine, None), "no local image at all");
        let old: Version = serde_json::from_str(r#"{"tag":"latest"}"#).unwrap();
        assert_eq!(old.image_id, None, "the old record still parses");
    }

    #[test]
    fn a_pre_release_sorts_below_its_release_and_above_the_one_before() {
        assert!(newer_than("v0.9.0-rc11", "v0.8.0"));
        assert!(newer_than("v0.9.0-rc12", "v0.9.0-rc11"));
        assert!(newer_than("v0.9.0", "v0.9.0-rc12"));
        assert!(!newer_than("v0.8.0", "v0.9.0-rc11"), "a stable release older than the installed rc is not an update");
        assert!(!newer_than("v0.9.0-rc11", "v0.9.0-rc11"));
        assert!(!newer_than("latest", "v0.9.0-rc11"), "the old fallback's record never wins");
        assert!(newer_than("v0.9.0-rc11", "latest"), "and is replaced by any real release");
    }

    #[test]
    fn the_updater_manifest_lives_on_the_release_not_on_latest() {
        assert_eq!(updater_endpoint("v0.9.0-rc6"), "https://github.com/DJG3DK/tektonix/releases/download/v0.9.0-rc6/latest.json");
    }

    #[test]
    fn preferences_default_to_automatic_without_prereleases() {
        let p = Prefs::default();
        assert!(p.auto_update && !p.include_prereleases);
        let back: Prefs = serde_json::from_str(&serde_json::to_string(&p).unwrap()).unwrap();
        assert!(back.auto_update);
    }

    #[test]
    fn a_secret_is_hinted_never_shown() {
        assert_eq!(hint("sk-or-v1-abcdefgh1234"), "••••••••1234");
        assert_eq!(hint("short"), "•••••");
    }
}


/// Whether the agent has nothing in flight: the public health route's
/// `busy` count. An agent too old to report it counts as busy, so an
/// automatic update never pulls a running task's containers out from
/// under it.
/// Whether the agent has a task in flight; None when it cannot be asked
/// (the stack is down, or not yet up).
pub async fn agent_busy() -> Option<bool> {
    let client = reqwest::Client::builder().timeout(std::time::Duration::from_secs(5)).build().ok()?;
    let r = client.get(HEALTH).send().await.ok()?;
    let v = r.json::<serde_json::Value>().await.ok()?;
    v.get("busy").and_then(|b| b.as_u64()).map(|b| b > 0)
}

pub async fn agent_idle() -> bool {
    agent_busy().await == Some(false)
}

/// Where the updater's manifest for a release lives: with the installer,
/// on that release. `releases/latest/download` would skip pre-releases.
pub fn updater_endpoint(tag: &str) -> String {
    format!("https://github.com/{REPO}/releases/download/{tag}/latest.json")
}

#[derive(Serialize, Clone)]
pub struct AppUpdate {
    pub available: bool,
    pub version: String,
    pub tag: String,
}

/// Is a newer app than this one attached to the newest release?
pub async fn check_app_update(app: &AppHandle) -> Result<AppUpdate, String> {
    use tauri_plugin_updater::UpdaterExt;
    let latest = latest_release(wants_prereleases(app)).await?;
    let endpoint: tauri::Url = updater_endpoint(&latest.tag_name).parse().map_err(|e: url::ParseError| e.to_string())?;
    let updater = app.updater_builder().endpoints(vec![endpoint]).map_err(|e| e.to_string())?
        .build().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(u)) => Ok(AppUpdate { available: true, version: u.version.clone(), tag: latest.tag_name }),
        Ok(None) => Ok(AppUpdate { available: false, version: String::new(), tag: latest.tag_name }),
        Err(e) => Err(format!("could not check the app's own update: {e}")),
    }
}

/// Download and install the newest app, then restart into it.
pub async fn install_app_update(app: &AppHandle) -> Result<(), String> {
    use tauri_plugin_updater::UpdaterExt;
    let latest = latest_release(wants_prereleases(app)).await?;
    let endpoint: tauri::Url = updater_endpoint(&latest.tag_name).parse().map_err(|e: url::ParseError| e.to_string())?;
    let updater = app.updater_builder().endpoints(vec![endpoint]).map_err(|e| e.to_string())?
        .build().map_err(|e| e.to_string())?;
    let Some(update) = updater.check().await.map_err(|e| e.to_string())? else {
        return Err("this app is already the newest".into());
    };
    note(app, format!("Downloading app {}...", update.version));
    update.download_and_install(|_, _| {}, || {}).await.map_err(|e| format!("app update failed: {e}"))?;
    note(app, "App updated; restarting.");
    app.restart();
}

/// One automatic pass: the stack when a newer release is out and the agent
/// is idle, then the app itself. Says what it did and why not.
pub async fn auto_update_pass(app: &AppHandle) -> Result<String, String> {
    // Whatever the preference says, the stack runs this app's release: a
    // newly installed app over an older stack corrects it here, once the
    // agent is idle, since the panel's Start is not offered while it runs.
    if ensure_own_release(app).await? {
        note(app, "Restarting the stack on its images.");
        up(app).await?;
    }
    let prefs = read_prefs(app);
    if !prefs.auto_update {
        return Ok("automatic updates are off".into());
    }
    let info = check_update(app).await?;
    if info.available {
        if !agent_idle().await {
            return Ok(format!("{} is out, waiting for the agent to be idle", info.latest));
        }
        note(app, format!("Updating the stack to {} (the agent is idle)", info.latest));
        update_to(app, &info.latest).await?;
    }
    // The app replaces itself only when nothing is going on: no task in
    // flight, and nobody at the window. 2026-09-29: it reinstalled itself
    // while the operator was typing to a running task, and took the window.
    let mine = check_app_update(app).await?;
    if mine.available {
        if !agent_idle().await {
            return Ok(format!("app {} is out; it installs when the agent is idle, or from the panel", mine.version));
        }
        if window_in_use(app) {
            return Ok(format!("app {} is out; it installs when this window is not in use, or from the panel", mine.version));
        }
        note(app, format!("A newer app ({}) is out; installing it", mine.version));
        install_app_update(app).await?;
    }
    Ok(if info.available { format!("updated the stack to {}", info.latest) } else { "up to date".into() })
}


/// The first account's password, chosen on the setup form instead of read
/// out of a container. The agent seeds the account with a one-time
/// password and requires a change before anything else; this does that
/// change through the same two routes a person would use: sign in with
/// the one-time password, then set the chosen one. Nothing is stored here.
/// Ok(false) when there is no one-time password to use (the account
/// already has its password), which is the normal case after the first
/// start.
pub async fn set_first_password(app: &AppHandle, email: &str, password: &str) -> Result<bool, String> {
    if password.trim().is_empty() {
        return Ok(false);
    }
    let one_time = match initial_password(app).await {
        Ok(p) if !p.is_empty() => p,
        _ => return Ok(false),
    };
    let client = reqwest::Client::builder()
        .cookie_store(true)
        .timeout(std::time::Duration::from_secs(20))
        .build().map_err(|e| e.to_string())?;
    let base = DASHBOARD;
    let login = client.post(format!("{base}/api/auth/login"))
        .json(&serde_json::json!({"email": email, "password": one_time}))
        .send().await.map_err(|e| format!("sign-in failed: {e}"))?;
    if !login.status().is_success() {
        return Err(format!("the agent refused the first sign-in ({})", login.status()));
    }
    let change = client.post(format!("{base}/api/auth/change-password"))
        .json(&serde_json::json!({"current_password": one_time, "new_password": password}))
        .send().await.map_err(|e| format!("setting the password failed: {e}"))?;
    if !change.status().is_success() {
        let body = change.text().await.unwrap_or_default();
        return Err(format!("the agent refused that password: {}", body.chars().take(200).collect::<String>()));
    }
    note(app, "Your password is set. Sign in to the console with it.");
    Ok(true)
}
