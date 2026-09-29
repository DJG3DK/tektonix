/// Every app command, so each gets an `allow-<command>` permission and the
/// capabilities can say which page may call which. Without this manifest
/// no capability could grant a command to the console page at :8100, and
/// its "Control panel" button was silently refused (2026-09-29). Kept equal
/// to lib.rs's generate_handler! by tests/test_desktop_app.py.
const COMMANDS: &[&str] = &[
    "docker_state",
    "docker_start",
    "docker_install",
    "settings_get",
    "settings_save",
    "machine_git_identity",
    "stack_dir",
    "installed_version",
    "stack_install",
    "stack_up",
    "stack_down",
    "stack_status",
    "stack_password",
    "stack_check_update",
    "stack_update",
    "prefs_get",
    "prefs_set",
    "app_version",
    "app_update_check",
    "app_update_install",
    "auto_update_now",
    "logs_follow",
    "logs_stop",
    "open_console",
    "open_panel",
    "open_projects_dir",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to run tauri-build");
}
