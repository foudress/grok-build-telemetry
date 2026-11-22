//! Desktop window for the local Token Telemetry dashboard.
//! The Python server in the repo stays the owner of the data and the UI.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod host;

use std::sync::Arc;
use std::thread;
use std::time::Duration;

use tauri::webview::WebviewWindow;
use tauri::{Manager, RunEvent, Theme, WebviewUrl, WebviewWindowBuilder};

use host::{dashboard_url, fit_portrait, log_line, resolve_root, stamp, venv_python, wait_healthy, Host, PORT};

fn main() {
    let (host, startup_error) = match open_host() {
        Ok(host) => (Arc::new(host), None),
        Err(message) => {
            log_line(&message);
            (
                Arc::new(Host {
                    root: std::path::PathBuf::new(),
                    python: std::path::PathBuf::new(),
                    child: std::sync::Mutex::new(None),
                    gate: std::sync::Mutex::new(()),
                }),
                Some(message),
            )
        }
    };
    let managed = Arc::clone(&host);

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .manage(managed)
        .setup(move |app| {
            let window = build_window(app.handle())?;
            dark_caption(&window);
            let app_handle = app.handle().clone();
            let host = Arc::clone(&host);
            thread::spawn(move || {
                if let Some(message) = startup_error {
                    set_status(&app_handle, &message);
                    return;
                }
                boot(&app_handle, &host);
                watch(&app_handle, &host);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to start Token Telemetry")
        .run(move |app, event| {
            if matches!(event, RunEvent::Exit) {
                if let Some(host) = app.try_state::<Arc<Host>>() {
                    host.stop();
                }
            }
        });
}

fn open_host() -> Result<Host, String> {
    let root = resolve_root().ok_or_else(|| {
        "Token Telemetry root was not found. Set GROK_TELEMETRY_ROOT or rebuild the app from the repo.".to_string()
    })?;
    let python = venv_python(&root).ok_or_else(|| {
        format!("No .venv Python under {}", root.display())
    })?;
    Ok(Host {
        root,
        python,
        child: std::sync::Mutex::new(None),
        gate: std::sync::Mutex::new(()),
    })
}

fn build_window(app: &tauri::AppHandle) -> tauri::Result<WebviewWindow> {
    let (width, height, x, y) = window_frame(app);
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Token Telemetry")
        .theme(Some(Theme::Dark))
        .background_color(tauri::window::Color(0x0b, 0x0f, 0x14, 255))
        .inner_size(width, height)
        .min_inner_size(360.0, 640.0)
        .position(x, y)
        .resizable(true)
        .build()
}

fn window_frame(app: &tauri::AppHandle) -> (f64, f64, f64, f64) {
    // Prefer the display closest to 9:16. A portrait monitor is the base view.
    let monitors = app.available_monitors().unwrap_or_default();
    let target = 9.0 / 16.0;
    let Some(monitor) = monitors.into_iter().min_by(|left, right| {
        monitor_delta(left, target)
            .partial_cmp(&monitor_delta(right, target))
            .unwrap_or(std::cmp::Ordering::Equal)
    }) else {
        let (width, height) = fit_portrait(1080.0, 1920.0);
        return (width, height, 80.0, 40.0);
    };
    let scale = monitor.scale_factor().max(1.0);
    let area = monitor.work_area();
    let avail_w = area.size.width as f64 / scale;
    let avail_h = area.size.height as f64 / scale;
    let (width, height) = fit_portrait(avail_w, avail_h);
    let x = area.position.x as f64 / scale + (avail_w - width) / 2.0;
    let y = area.position.y as f64 / scale + (avail_h - height) / 2.0;
    log_line(&format!(
        "frame {width:.0}x{height:.0} at {x:.0},{y:.0} on {}x{}",
        area.size.width, area.size.height
    ));
    (width, height, x, y)
}

fn monitor_delta(monitor: &tauri::Monitor, target: f64) -> f64 {
    let scale = monitor.scale_factor().max(1.0);
    let area = monitor.work_area();
    let width = area.size.width as f64 / scale;
    let height = area.size.height as f64 / scale;
    if height < 1.0 {
        return f64::MAX;
    }
    (width / height - target).abs()
}

fn boot(app: &tauri::AppHandle, host: &Host) {
    set_status(app, "Starting the local dashboard…");
    if let Err(message) = host.start() {
        log_line(&message);
        set_status(app, &message);
        return;
    }
    if !wait_healthy(Duration::from_secs(60)) {
        let tail = host::err_tail();
        let message = if tail.is_empty() {
            format!("The dashboard did not answer on port {PORT}.")
        } else {
            format!("The dashboard did not answer on port {PORT}.\n{tail}")
        };
        log_line(&message);
        set_status(app, &message);
        return;
    }
    let url = dashboard_url(&host.python, &host.root);
    log_line(&format!("open {url}"));
    navigate(app, &url);
}

fn watch(app: &tauri::AppHandle, host: &Host) {
    let mut seen = stamp(&host.root);
    loop {
        thread::sleep(Duration::from_millis(1200));
        if app.get_webview_window("main").is_none() {
            break;
        }
        let next = stamp(&host.root);
        if next == seen {
            continue;
        }
        thread::sleep(Duration::from_millis(600));
        let settled = stamp(&host.root);
        if settled == seen {
            continue;
        }
        let code_changed = settled.code != seen.code;
        seen = settled;
        if code_changed {
            log_line("repo python changed — restarting the dashboard");
            boot(app, host);
        } else if let Some(window) = app.get_webview_window("main") {
            log_line("repo dashboard changed — reloading");
            let _ = window.eval("location.reload()");
        }
    }
}

fn navigate(app: &tauri::AppHandle, url: &str) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    match url.parse() {
        Ok(parsed) => {
            if let Err(err) = window.navigate(parsed) {
                log_line(&format!("navigate failed: {err}"));
                set_status(app, "The window could not open the dashboard.");
            }
        }
        Err(err) => {
            log_line(&format!("bad dashboard url {url}: {err}"));
        }
    }
}

fn set_status(app: &tauri::AppHandle, message: &str) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let quoted = serde_json::to_string(message).unwrap_or_else(|_| "\"\"".to_string());
    let _ = window.eval(&format!("setStatus({quoted})"));
}

fn dark_caption(window: &WebviewWindow) {
    #[cfg(windows)]
    apply_dark_caption(window);
}

#[cfg(windows)]
fn apply_dark_caption(window: &WebviewWindow) {
    let Ok(hwnd) = window.hwnd() else {
        return;
    };
    let raw = hwnd.0 as isize;
    let root = unsafe { GetAncestor(raw, 2) };
    let frame = if root == 0 { raw } else { root };
    unsafe {
        let dark: i32 = 1;
        // 19 = Windows 10 20H1, 20 = later builds. Caption/text force dark
        // even when the system theme is light. COLORREF is 0x00BBGGRR.
        DwmSetWindowAttribute(frame, 19, &dark as *const i32 as *const _, 4);
        DwmSetWindowAttribute(frame, 20, &dark as *const i32 as *const _, 4);
        let caption: u32 = 0x0014_0f0b;
        let text: u32 = 0x00f3_ede6;
        DwmSetWindowAttribute(frame, 35, &caption as *const u32 as *const _, 4);
        DwmSetWindowAttribute(frame, 36, &text as *const u32 as *const _, 4);
    }
}

#[cfg(windows)]
#[link(name = "user32")]
extern "system" {
    fn GetAncestor(hwnd: isize, flags: u32) -> isize;
}

#[cfg(windows)]
#[link(name = "dwmapi")]
extern "system" {
    fn DwmSetWindowAttribute(
        hwnd: isize,
        attribute: u32,
        value: *const std::ffi::c_void,
        size: u32,
    ) -> i32;
}

