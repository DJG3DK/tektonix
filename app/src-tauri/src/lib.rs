//! Tektonix desktop: the dashboard in a window, the compose stack under it,
//! and a tray icon to reach both. The window's own page (src/) is a small
//! control panel; the dashboard itself is the agent's web app, opened in a
//! second window once the stack answers. See stack.rs for the layout.

mod docker;
mod proc;
mod stack;

use std::sync::Arc;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tokio::sync::Mutex;

struct LogFollow(Mutex<Option<proc::Streaming>>);

#[tauri::command]
async fn docker_state() -> docker::DockerState {
    docker::state().await
}

#[tauri::command]
async fn docker_start(app: AppHandle) -> Result<(), String> {
    docker::start_and_wait(&app).await
}

#[tauri::command]
async fn docker_install(app: AppHandle) -> Result<String, String> {
    docker::install(&app).await
}

#[tauri::command]
fn settings_get(app: AppHandle) -> Result<stack::Settings, String> {
    stack::prepare(&app)?;
    stack::read_settings(&app)
}

#[tauri::command]
fn settings_save(app: AppHandle, key: Option<String>, projects_dir: String, admin_email: String,
                 git_name: String, git_email: String) -> Result<stack::Settings, String> {
    stack::prepare(&app)?;
    stack::save_settings(&app, key, projects_dir, admin_email, git_name, git_email)
}

#[tauri::command]
async fn machine_git_identity() -> (String, String) {
    stack::machine_git_identity().await
}

#[tauri::command]
fn stack_dir(app: AppHandle) -> Result<String, String> {
    Ok(stack::dir(&app)?.display().to_string())
}

#[tauri::command]
fn installed_version(app: AppHandle) -> Option<String> {
    stack::installed_version(&app)
}

/// First start: pull the release this app was built for, then up.
#[tauri::command]
async fn stack_install(app: AppHandle) -> Result<(), String> {
    stack::prepare(&app)?;
    let tag = format!("v{}", app.package_info().version);
    stack::pull(&app, &tag).await?;
    stack::up(&app).await
}

#[tauri::command]
async fn stack_up(app: AppHandle) -> Result<(), String> {
    stack::prepare(&app)?;
    if stack::installed_version(&app).is_none() {
        let tag = format!("v{}", app.package_info().version);
        stack::pull(&app, &tag).await?;
    }
    stack::up(&app).await
}

#[tauri::command]
async fn stack_down(app: AppHandle) -> Result<(), String> {
    stack::down(&app).await
}

#[tauri::command]
async fn stack_status(app: AppHandle) -> Result<Vec<stack::Container>, String> {
    stack::status(&app).await
}

#[tauri::command]
async fn stack_password(app: AppHandle) -> Result<String, String> {
    stack::initial_password(&app).await
}

#[tauri::command]
async fn stack_check_update(app: AppHandle) -> Result<stack::UpdateInfo, String> {
    stack::check_update(&app).await
}

#[tauri::command]
async fn stack_update(app: AppHandle, tag: String) -> Result<(), String> {
    stack::update_to(&app, &tag).await
}

#[tauri::command]
fn prefs_get(app: AppHandle) -> stack::Prefs {
    stack::read_prefs(&app)
}

#[tauri::command]
fn prefs_set(app: AppHandle, auto_update: bool, include_prereleases: bool) -> Result<stack::Prefs, String> {
    stack::write_prefs(&app, &stack::Prefs { auto_update, include_prereleases })
}

#[tauri::command]
async fn app_update_check(app: AppHandle) -> Result<stack::AppUpdate, String> {
    stack::check_app_update(&app).await
}

#[tauri::command]
async fn app_update_install(app: AppHandle) -> Result<(), String> {
    stack::install_app_update(&app).await
}

#[tauri::command]
async fn auto_update_now(app: AppHandle) -> Result<String, String> {
    stack::auto_update_pass(&app).await
}

/// Automatic updates: two minutes after start, then every six hours. Each
/// pass is one line in the log pane; a failure is a line too, never a dialog.
fn spawn_auto_updater(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(120)).await;
        loop {
            match stack::auto_update_pass(&app).await {
                Ok(what) => { let _ = app.emit(proc::LOG_EVENT, proc::LogLine { stream: "app".into(), line: format!("Update check: {what}.") }); }
                Err(e) => { let _ = app.emit(proc::LOG_EVENT, proc::LogLine { stream: "app".into(), line: format!("Update check failed: {e}") }); }
            }
            tokio::time::sleep(std::time::Duration::from_secs(6 * 3600)).await;
        }
    });
}

#[tauri::command]
async fn logs_follow(app: AppHandle, service: String, state: tauri::State<'_, Arc<LogFollow>>) -> Result<(), String> {
    logs_stop(state.clone()).await?;
    let dir = stack::dir(&app)?;
    let f = dir.join("docker-compose.yml").display().to_string();
    let d = dir.display().to_string();
    let args = ["compose", "--project-directory", d.as_str(), "-f", f.as_str(), "logs", "-f", "--tail", "200", service.as_str()];
    let child = proc::spawn_streaming(&app, "logs", "docker", &args, Some(&dir)).map_err(|e| e.to_string())?;
    *state.0.lock().await = Some(child);
    Ok(())
}

#[tauri::command]
async fn logs_stop(state: tauri::State<'_, Arc<LogFollow>>) -> Result<(), String> {
    if let Some(child) = state.0.lock().await.take() {
        child.stop().await;
    }
    Ok(())
}

#[tauri::command]
async fn open_dashboard(app: AppHandle) -> Result<(), String> {
    show_dashboard(&app)
}

fn show_dashboard(app: &AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("dashboard") {
        let _ = w.show();
        let _ = w.set_focus();
        return Ok(());
    }
    let url: tauri::Url = stack::DASHBOARD.parse().map_err(|e: url::ParseError| e.to_string())?;
    WebviewWindowBuilder::new(app, "dashboard", WebviewUrl::External(url))
        .title("Tektonix")
        .inner_size(1360.0, 860.0)
        .min_inner_size(900.0, 600.0)
        // Frameless; the dashboard draws its own title strip when it runs in
        // this window (frontend/src/components/TitleBar.tsx) and gets the
        // window controls through capabilities/dashboard.json.
        .decorations(false)
        .build()
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn show_panel(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open dashboard", true, None::<&str>)?;
    let panel = MenuItem::with_id(app, "panel", "Control panel", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit (the stack keeps running)", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &panel, &quit])?;
    let icon = app.default_window_icon().cloned().expect("the bundle has an icon");
    TrayIconBuilder::with_id("tektonix")
        .icon(icon)
        .tooltip("Tektonix")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => { let _ = show_dashboard(app); }
            "panel" => show_panel(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        // One running copy. A second launch (the installer's "run when
        // finished" plus the Start menu, or a launch while the first sits in
        // the tray) brings the running panel forward instead of starting
        // another app with a dead taskbar button of its own (2026-09-29).
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_panel(app)))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Arc::new(LogFollow(Mutex::new(None))))
        .setup(|app| {
            build_tray(app.handle())?;
            spawn_auto_updater(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the panel hides it; the tray keeps the app alive, and
            // the stack runs regardless. Quit is in the tray menu.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            docker_state, docker_start, docker_install,
            settings_get, settings_save, machine_git_identity, stack_dir, installed_version,
            stack_install, stack_up, stack_down, stack_status, stack_password,
            stack_check_update, stack_update, prefs_get, prefs_set,
            app_update_check, app_update_install, auto_update_now,
            logs_follow, logs_stop, open_dashboard,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tektonix");
}
