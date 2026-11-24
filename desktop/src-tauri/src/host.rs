//! Owns the local dashboard process. The window is only a shell.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const PORT: u16 = 8765;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stamp {
    pub ui: u64,
    pub code: u64,
}

pub struct Host {
    pub root: PathBuf,
    pub python: PathBuf,
    pub child: std::sync::Mutex<Option<Child>>,
    pub gate: std::sync::Mutex<()>,
}

pub fn fit_portrait(avail_w: f64, avail_h: f64) -> (f64, f64) {
    let margin_w = avail_w * 0.98;
    let margin_h = avail_h * 0.98;
    let mut height = margin_h;
    let mut width = height * 9.0 / 16.0;
    if width > margin_w {
        width = margin_w;
        height = width * 16.0 / 9.0;
    }
    (width.floor().max(360.0), height.floor().max(640.0))
}

pub fn with_shell(url: &str) -> String {
    if url.contains("shell=app") {
        return url.to_string();
    }
    let join = if url.contains('?') { '&' } else { '?' };
    format!("{url}{join}shell=app")
}

pub fn pick_root(candidates: &[Option<PathBuf>]) -> Option<PathBuf> {
    candidates.iter().flatten().find(|path| path.join("pyproject.toml").is_file()).cloned()
}

pub fn resolve_root() -> Option<PathBuf> {
    let bundled = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("runtime")));
    let env = std::env::var("GROK_TELEMETRY_ROOT").ok().map(PathBuf::from);
    let marker = install_root();
    let baked = option_env!("TT_REPO_ROOT").map(PathBuf::from);
    pick_root(&[bundled, env, marker, baked])
}

fn install_root() -> Option<PathBuf> {
    let home = std::env::var("USERPROFILE").ok()?;
    let marker = PathBuf::from(home).join(".grok-telemetry").join("install.json");
    let text = fs::read_to_string(marker).ok()?;
    let data: serde_json::Value = serde_json::from_str(&text).ok()?;
    data.get("root").and_then(|v| v.as_str()).map(PathBuf::from)
}

pub fn resolve_python(root: &Path) -> Option<PathBuf> {
    [
        root.join("python").join("python.exe"),
        root.join(".venv").join("Scripts").join("python.exe"),
    ]
    .into_iter()
    .find(|path| path.is_file())
}

pub fn dashboard_url(python: &Path, root: &Path) -> String {
    let code = r#"
import os
import launch_dashboard
source, sid = launch_dashboard.launch_choice(session_id="")
print(launch_dashboard.dashboard_url(int(os.environ["TT_PORT"]), source, sid), end="")
"#;
    let output = Command::new(python)
        .arg("-c")
        .arg(code)
        .current_dir(root)
        .env("TT_PORT", PORT.to_string())
        .output();
    if let Ok(output) = output {
        if output.status.success() {
            let url = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if url.starts_with("http://") {
                return with_shell(&url);
            }
        }
    }
    with_shell(&format!("http://127.0.0.1:{PORT}/?source=grok"))
}

pub fn stamp(root: &Path) -> Stamp {
    Stamp {
        ui: walk_hash(root, &root.join("dashboard"), false),
        code: walk_hash(root, &root.join("token_telemetry"), true)
            ^ file_hash(root, &root.join("launch_dashboard.py")),
    }
}

fn walk_hash(root: &Path, dir: &Path, python_only: bool) -> u64 {
    let mut acc = 0u64;
    let mut stack = vec![dir.to_path_buf()];
    while let Some(current) = stack.pop() {
        let entries = match fs::read_dir(&current) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if path.is_dir() {
                if name == "__pycache__" || name == ".venv" || name == "target" {
                    continue;
                }
                stack.push(path);
                continue;
            }
            if python_only && path.extension().and_then(|ext| ext.to_str()) != Some("py") {
                continue;
            }
            if matches!(path.extension().and_then(|ext| ext.to_str()), Some("pyc" | "pyo")) {
                continue;
            }
            acc ^= file_hash(root, &path);
        }
    }
    acc
}

fn file_hash(root: &Path, path: &Path) -> u64 {
    let meta = match fs::metadata(path) {
        Ok(meta) => meta,
        Err(_) => return 0,
    };
    let modified = meta
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|time| time.as_millis())
        .unwrap_or(0);
    let rel = path.strip_prefix(root).unwrap_or(path);
    mix(rel.to_string_lossy().as_bytes(), modified, meta.len())
}

fn mix(path: &[u8], modified: u128, len: u64) -> u64 {
    let mut hash = len.wrapping_mul(0x9E37_79B1_85EB_CA87) ^ (modified as u64);
    for byte in path {
        hash = hash.wrapping_mul(131).wrapping_add(*byte as u64);
    }
    hash
}

impl Host {
    pub fn start(&self) -> Result<(), String> {
        let _guard = self.gate.lock().map_err(|_| "restart lock poisoned".to_string())?;
        self.stop_locked();
        kill_listeners(PORT);
        let mut child_slot = self.child.lock().map_err(|_| "child lock poisoned".to_string())?;
        *child_slot = Some(spawn_server(&self.python, &self.root)?);
        Ok(())
    }

    pub fn stop(&self) {
        if let Ok(_guard) = self.gate.lock() {
            self.stop_locked();
        }
    }

    fn stop_locked(&self) {
        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut child) = slot.take() {
                stop_child(&mut child);
            }
        }
    }
}

fn stop_child(child: &mut Child) {
    // The venv python.exe on Windows respawns the base interpreter. Killing
    // only the direct child leaves that interpreter, and the port, alive.
    let _ = Command::new("taskkill")
        .args(["/F", "/T", "/PID", &child.id().to_string()])
        .output();
    let _ = child.kill();
    let _ = child.wait();
}

fn spawn_server(python: &Path, root: &Path) -> Result<Child, String> {
    let dir = log_dir();
    fs::create_dir_all(&dir).map_err(|err| err.to_string())?;
    let stdout = File::create(dir.join("desktop.out.log")).map_err(|err| err.to_string())?;
    let stderr = File::create(dir.join("desktop.err.log")).map_err(|err| err.to_string())?;
    let mut cmd = Command::new(python);
    cmd.arg("-u")
        .arg("-m")
        .arg("token_telemetry")
        .arg("--port")
        .arg(PORT.to_string())
        .arg("--no-open")
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    cmd.spawn().map_err(|err| format!("could not start the dashboard: {err}"))
}

pub fn wait_healthy(timeout: Duration) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if health_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

fn health_ok() -> bool {
    let address = format!("127.0.0.1:{PORT}");
    let Ok(addr) = address.parse() else {
        return false;
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(400)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!("GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 512];
    let size = stream.read(&mut buf).unwrap_or(0);
    let text = String::from_utf8_lossy(&buf[..size]);
    text.contains(" 200")
}

fn kill_listeners(port: u16) {
    for pid in listening_pids(port) {
        if pid == std::process::id() {
            continue;
        }
        let _ = Command::new("taskkill")
            .args(["/F", "/T", "/PID", &pid.to_string()])
            .output();
    }
    let start = std::time::Instant::now();
    while start.elapsed() < Duration::from_secs(3) && !listening_pids(port).is_empty() {
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn listening_pids(port: u16) -> Vec<u32> {
    let Ok(output) = Command::new("netstat").args(["-ano", "-p", "TCP"]).output() else {
        return Vec::new();
    };
    let text = String::from_utf8_lossy(&output.stdout);
    let mut pids = Vec::new();
    for line in text.lines() {
        let cols: Vec<&str> = line.split_whitespace().collect();
        if cols.len() < 5 || !cols[3].eq_ignore_ascii_case("LISTENING") {
            continue;
        }
        if !local_port_is(cols[1], port) {
            continue;
        }
        if let Ok(pid) = cols[4].parse::<u32>() {
            if !pids.contains(&pid) {
                pids.push(pid);
            }
        }
    }
    pids
}

fn local_port_is(addr: &str, port: u16) -> bool {
    addr.rsplit_once(':')
        .and_then(|(_, value)| value.parse::<u16>().ok())
        == Some(port)
}

fn log_dir() -> PathBuf {
    std::env::var("USERPROFILE")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join(".grok-telemetry")
}

pub fn log_line(message: &str) {
    let dir = log_dir();
    let _ = fs::create_dir_all(&dir);
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(dir.join("desktop.log")) {
        let _ = writeln!(file, "{stamp} {message}");
    }
}

pub fn err_tail() -> String {
    let path = log_dir().join("desktop.err.log");
    let Ok(text) = fs::read_to_string(path) else {
        return String::new();
    };
    let tail: String = text.chars().rev().take(400).collect();
    tail.chars().rev().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn portrait_uses_nine_by_sixteen_on_a_tall_screen() {
        let (width, height) = fit_portrait(1200.0, 1600.0);
        assert!((width / height - 9.0 / 16.0).abs() < 0.01, "{width}x{height}");
        assert!(width < 1200.0);
    }

    #[test]
    fn portrait_shrinks_to_a_narrow_screen() {
        let (width, height) = fit_portrait(400.0, 2000.0);
        assert!(width <= 400.0 * 0.98 + 1.0);
        assert!((width / height - 9.0 / 16.0).abs() < 0.02, "{width}x{height}");
    }

    #[test]
    fn shell_query_is_appended_once() {
        assert_eq!(
            with_shell("http://127.0.0.1:8765/?source=grok&session=abc"),
            "http://127.0.0.1:8765/?source=grok&session=abc&shell=app"
        );
        assert_eq!(
            with_shell("http://127.0.0.1:8765/?shell=app"),
            "http://127.0.0.1:8765/?shell=app"
        );
    }

    #[test]
    fn root_requires_the_project_file() {
        let dir = std::env::temp_dir().join(format!("tt-root-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        assert!(pick_root(&[Some(dir.clone())]).is_none());
        fs::write(dir.join("pyproject.toml"), "[project]\nname='x'\n").unwrap();
        assert_eq!(pick_root(&[Some(dir.clone())]).as_ref(), Some(&dir));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn stamp_ignores_pyc_and_sees_a_dashboard_edit() {
        let dir = std::env::temp_dir().join(format!("tt-stamp-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("dashboard")).unwrap();
        fs::create_dir_all(dir.join("token_telemetry").join("__pycache__")).unwrap();
        fs::write(dir.join("dashboard").join("index.html"), "a").unwrap();
        fs::write(dir.join("token_telemetry").join("live.py"), "print(1)\n").unwrap();
        fs::write(dir.join("token_telemetry").join("__pycache__").join("live.pyc"), "nope").unwrap();
        let first = stamp(&dir);
        fs::write(dir.join("dashboard").join("index.html"), "b").unwrap();
        let second = stamp(&dir);
        assert_ne!(first.ui, second.ui);
        assert_eq!(first.code, second.code);
        let _ = fs::remove_dir_all(&dir);
    }
}
