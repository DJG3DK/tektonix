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
use tauri::{AppHandle, Emitter, Manager};
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

/// First start: pull the release this app was built for, up, and set the
/// password chosen on the form (nothing if the account already has one).
#[tauri::command]
async fn stack_install(app: AppHandle, password: Option<String>) -> Result<bool, String> {
    stack::prepare(&app)?;
    stack::pull(&app, &stack::release_tag(&app)).await?;
    stack::up(&app).await?;
    let email = stack::read_settings(&app)?.admin_email;
    match password {
        Some(p) => stack::set_first_password(&app, &email, &p).await,
        None => Ok(false),
    }
}

#[tauri::command]
async fn stack_up(app: AppHandle) -> Result<(), String> {
    stack::prepare(&app)?;
    stack::ensure_own_release(&app).await?;
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

/// One window, two pages. The console (the agent's own web page) and the
/// control panel (this app's page) take turns in the main window: the
/// console when the stack answers, the panel before that and whenever it
/// is asked for. Navigating the window, rather than framing the console
/// in the panel, keeps the console's sign-in cookie first-party.
#[tauri::command]
async fn open_console(app: AppHandle) -> Result<(), String> {
    show_console(&app)
}

#[tauri::command]
async fn open_panel(app: AppHandle) -> Result<(), String> {
    show_panel_page(&app)
}

fn show_console(app: &AppHandle) -> Result<(), String> {
    let w = app.get_webview_window("main").ok_or("no window")?;
    let url: tauri::Url = stack::DASHBOARD.parse().map_err(|e: url::ParseError| e.to_string())?;
    w.navigate(url).map_err(|e| e.to_string())?;
    let _ = w.show();
    let _ = w.set_focus();
    Ok(())
}

fn show_panel_page(app: &AppHandle) -> Result<(), String> {
    let w = app.get_webview_window("main").ok_or("no window")?;
    let url: tauri::Url = "tauri://localhost/index.html".parse().map_err(|e: url::ParseError| e.to_string())?;
    // On Windows the app's own pages are served from http://tauri.localhost.
    let url = if cfg!(windows) { "http://tauri.localhost/index.html".parse().map_err(|e: url::ParseError| e.to_string())? } else { url };
    w.navigate(url).map_err(|e| e.to_string())?;
    let _ = w.show();
    let _ = w.set_focus();
    Ok(())
}

fn show_panel(app: &AppHandle) {
    if show_panel_page(app).is_err() {
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.show();
            let _ = w.set_focus();
        }
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open console", true, None::<&str>)?;
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
            "open" => { let _ = show_console(app); }
            "panel" => show_panel(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

/// Runs on every page the window shows, the app's own and the console's.
/// A frameless window is only usable through a strip drawn by the page it
/// shows. The app's page and a current console draw one; an older console
/// draws nothing, and the window could then neither be moved nor closed
/// (2026-09-29). So: when the page has drawn no strip, draw one here.
const STRIP_FALLBACK: &str = r#"
(function () {
  function ensure() {
    if (document.querySelector('.titlebar, .top[data-tauri-drag-region]')) return;
    var w = window.__TAURI__ && window.__TAURI__.window && window.__TAURI__.window.getCurrentWindow && window.__TAURI__.window.getCurrentWindow();
    if (!w) return;
    var bar = document.createElement('div');
    bar.setAttribute('data-tauri-drag-region', '');
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;height:30px;z-index:2147483647;display:flex;align-items:center;justify-content:space-between;padding-left:12px;background:#171b21;color:#e6e9ee;border-bottom:1px solid #2a313b;font:12px system-ui,sans-serif;user-select:none;-webkit-user-select:none;';
    var name = document.createElement('span'); name.textContent = 'Tektonix'; name.setAttribute('data-tauri-drag-region', ''); name.style.fontWeight = '600';
    var ctl = document.createElement('span'); ctl.style.cssText = 'display:flex;height:100%;';
    function btn(label, title, fn, close) {
      var b = document.createElement('button'); b.textContent = label; b.title = title;
      b.style.cssText = 'width:40px;height:100%;border:0;background:transparent;color:inherit;font:inherit;cursor:pointer;';
      b.onmouseenter = function () { b.style.background = close ? '#d9534f' : 'rgba(255,255,255,.08)'; };
      b.onmouseleave = function () { b.style.background = 'transparent'; };
      b.onclick = fn; return b;
    }
    var panel = btn('Control panel', 'Back to the control panel', function () { window.__TAURI__.core.invoke('open_panel'); });
    panel.style.width = 'auto'; panel.style.padding = '0 10px'; panel.style.opacity = '.75';
    ctl.appendChild(panel);
    ctl.appendChild(btn('\u2500', 'Minimise', function () { w.minimize(); }));
    ctl.appendChild(btn('\u25A1', 'Maximise', function () { w.toggleMaximize(); }));
    ctl.appendChild(btn('\u2715', 'Close', function () { w.close(); }, true));
    bar.appendChild(name); bar.appendChild(ctl);
    document.documentElement.appendChild(bar);
    document.documentElement.style.setProperty('--tektonix-strip', '30px');
    document.body && (document.body.style.paddingTop = '30px');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(ensure, 300); });
  else setTimeout(ensure, 300);
})();
"#;

fn build_main_window(app: &AppHandle) -> tauri::Result<()> {
    tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
        .title("Tektonix")
        .inner_size(1360.0, 860.0)
        .min_inner_size(900.0, 600.0)
        .decorations(false)
        .initialization_script(STRIP_FALLBACK)
        .build()?;
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
            build_main_window(app.handle())?;
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
            logs_follow, logs_stop, open_console, open_panel,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tektonix");
}
