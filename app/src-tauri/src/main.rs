// Prevents an extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // Linux: WebKitGTK's DMA-BUF renderer needs libGLESv2, which the
    // AppImage cannot carry (it is the system's GL dispatcher) and a desktop
    // without libgles2 does not have. There the app aborted before drawing
    // a window (2026-10-04, an Ubuntu 24.04 XFCE install); it is also the
    // renderer behind the blank windows NVIDIA drivers give WebKitGTK apps.
    // Set before anything starts a thread, and only when the person has not
    // chosen otherwise.
    #[cfg(target_os = "linux")]
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
    tektonix_lib::run()
}
