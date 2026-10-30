#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

const BACKEND_PORT: u16 = 8787;
const MIGRATED_FILES: &[&str] = &[
    ".env",
    "credentials.json",
    "token.json",
    "gmail_token.json",
    "gmail_cache.sqlite3",
    "gmail_cache.sqlite3-shm",
    "gmail_cache.sqlite3-wal",
    "productivity.json",
    "tasks.json",
    "tasks_backup.json",
    "whatsapp_state.sqlite3",
    "whatsapp_state.sqlite3-shm",
    "whatsapp_state.sqlite3-wal",
];

struct BackendProcess {
    child: Mutex<Option<Child>>,
    backend_path: PathBuf,
    data_dir: PathBuf,
    log_path: PathBuf,
    start_lock: Mutex<()>,
}

impl BackendProcess {
    fn ensure_running(&self) -> Result<(), String> {
        if backend_health_check() {
            return Ok(());
        }

        let _start_guard = self
            .start_lock
            .lock()
            .map_err(|_| "Backend startup is unavailable; restart the application.".to_string())?;
        if backend_health_check() {
            return Ok(());
        }

        let mut child_slot = self
            .child
            .lock()
            .map_err(|_| "Backend process state is unavailable.".to_string())?;
        if let Some(child) = child_slot.as_mut() {
            match child.try_wait() {
                Ok(None) => {
                    return Err(
                        "The backend process is running but not healthy. Check backend.log in the application data directory."
                            .to_string(),
                    )
                }
                Ok(Some(status)) => {
                    write_diagnostic(
                        &self.log_path,
                        &format!("Python backend exited ({status}); a manual retry was requested."),
                    );
                    *child_slot = None;
                }
                Err(error) => {
                    return Err(format!(
                        "Could not inspect backend process ({:?}).",
                        error.kind()
                    ))
                }
            }
        }

        if !self.backend_path.is_file() {
            write_diagnostic(
                &self.log_path,
                &format!(
                    "Python backend executable is missing: {}",
                    self.backend_path.display()
                ),
            );
            return Err(
                "The packaged Python backend executable is missing. Reinstall the application and check backend.log."
                    .to_string(),
            );
        }

        let mut command = Command::new(&self.backend_path);
        command
            .current_dir(&self.data_dir)
            .env("PERSONAL_AI_AGENT_DATA_DIR", &self.data_dir)
            .stdin(Stdio::null());
        if cfg!(debug_assertions) {
            command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
        } else {
            command.stdout(Stdio::null()).stderr(Stdio::null());
        }

        let mut child = command.spawn().map_err(|error| {
            let message = format!("Could not start Python backend ({:?}).", error.kind());
            write_diagnostic(&self.log_path, &message);
            message
        })?;
        if let Err(error) = wait_for_backend(&mut child) {
            write_diagnostic(&self.log_path, &error);
            return Err(error);
        }

        *child_slot = Some(child);
        write_diagnostic(&self.log_path, "Python backend is healthy.");
        Ok(())
    }
}

fn write_diagnostic(path: &Path, message: &str) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut log) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(log, "{message}");
    }
    #[cfg(debug_assertions)]
    eprintln!("{message}");
}

fn copy_if_missing(source: &Path, destination: &Path) -> std::io::Result<bool> {
    if !source.is_file() || destination.exists() {
        return Ok(false);
    }
    let mut input = File::open(source)?;
    let mut output = match OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return Ok(false),
        Err(error) => return Err(error),
    };
    if let Err(error) = std::io::copy(&mut input, &mut output).and_then(|_| output.sync_all()) {
        drop(output);
        let _ = fs::remove_file(destination);
        return Err(error);
    }
    Ok(true)
}

fn migrate_legacy_data(data_dir: &Path, executable_dir: &Path) {
    let mut roots = vec![
        executable_dir.join("resources"),
        executable_dir.to_path_buf(),
    ];
    if let Ok(current_dir) = std::env::current_dir() {
        roots.push(current_dir.join("resources"));
        roots.push(current_dir.clone());
    }
    #[cfg(debug_assertions)]
    {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        if let Some(repository_dir) = manifest_dir.parent().and_then(Path::parent) {
            roots.push(repository_dir.join("resources"));
            roots.push(repository_dir.to_path_buf());
        }
    }

    for root in roots {
        if root == data_dir {
            continue;
        }
        for name in MIGRATED_FILES {
            let source = root.join(name);
            let destination = data_dir.join(name);
            match copy_if_missing(&source, &destination) {
                Ok(true) => write_diagnostic(
                    &data_dir.join("backend.log"),
                    &format!("Migrated existing local file: {name}"),
                ),
                Ok(false) => {}
                Err(error) => write_diagnostic(
                    &data_dir.join("backend.log"),
                    &format!(
                        "Could not migrate {name} ({:?}). Existing files were left untouched.",
                        error.kind()
                    ),
                ),
            }
        }
    }
}

fn backend_health_check() -> bool {
    let address = match (("127.0.0.1", BACKEND_PORT)).to_socket_addrs() {
        Ok(mut addresses) => match addresses.next() {
            Some(address) => address,
            None => return false,
        },
        Err(_) => return false,
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(300)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    if stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok()
        && response.starts_with("HTTP/1.1 200")
        && response.contains("\"status\":\"ok\"")
        && response.contains("\"timezone\":\"Africa/Harare\"")
}

fn wait_for_backend(child: &mut Child) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(status)) => return Err(format!("Backend exited during startup ({status}).")),
            Err(error) => {
                return Err(format!(
                    "Could not inspect backend startup ({:?}).",
                    error.kind()
                ))
            }
            Ok(None) => {}
        }
        if backend_health_check() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
    let _ = child.kill();
    let _ = child.wait();
    Err("Backend did not become healthy within 15 seconds.".to_string())
}

fn resolve_backend_path(executable_dir: &Path) -> PathBuf {
    let packaged = executable_dir.join("api.exe");
    if packaged.is_file() {
        return packaged;
    }

    #[cfg(debug_assertions)]
    {
        let development = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join("api-x86_64-pc-windows-msvc.exe");
        if development.is_file() {
            return development;
        }
    }

    packaged
}

fn monitor_backend(process: Arc<BackendProcess>) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(2));
        let mut child_slot = match process.child.lock() {
            Ok(child) => child,
            Err(_) => {
                write_diagnostic(
                    &process.log_path,
                    "Backend process monitor could not access its process state.",
                );
                break;
            }
        };
        let Some(child) = child_slot.as_mut() else {
            continue;
        };
        match child.try_wait() {
            Ok(Some(status)) => {
                *child_slot = None;
                write_diagnostic(
                    &process.log_path,
                    &format!("Python backend stopped unexpectedly ({status})."),
                );
            }
            Ok(None) => {}
            Err(error) => {
                write_diagnostic(
                    &process.log_path,
                    &format!("Could not monitor Python backend ({:?}).", error.kind()),
                );
            }
        }
    });
}

#[tauri::command]
fn retry_backend(process: tauri::State<'_, Arc<BackendProcess>>) -> Result<(), String> {
    process.ensure_running()
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct WidgetPreferences {
    x: Option<i32>,
    y: Option<i32>,
    width: u32,
    height: u32,
    always_on_top: bool,
}

impl Default for WidgetPreferences {
    fn default() -> Self {
        Self {
            x: None,
            y: None,
            width: 360,
            height: 430,
            always_on_top: false,
        }
    }
}

fn widget_preferences_path(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|path| path.join("widget-preferences.json"))
        .map_err(|_| "Could not resolve the application data directory.".to_string())
}

#[tauri::command]
fn get_widget_preferences(app: AppHandle) -> Result<WidgetPreferences, String> {
    let path = widget_preferences_path(&app)?;
    match fs::read_to_string(path) {
        Ok(contents) => serde_json::from_str(&contents)
            .map_err(|_| "Widget preferences are invalid; reset the preferences file.".to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Ok(WidgetPreferences::default())
        }
        Err(_) => Err("Widget preferences could not be read.".to_string()),
    }
}

#[tauri::command]
fn save_widget_preferences(app: AppHandle, preferences: WidgetPreferences) -> Result<(), String> {
    let path = widget_preferences_path(&app)?;
    let parent = path
        .parent()
        .ok_or_else(|| "Could not resolve the application data directory.".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|_| "Could not create the application data directory.".to_string())?;
    let contents = serde_json::to_vec_pretty(&preferences)
        .map_err(|_| "Widget preferences could not be serialized.".to_string())?;
    fs::write(path, contents).map_err(|_| "Widget preferences could not be saved.".to_string())
}

#[tauri::command]
fn open_widget(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("planner-widget") {
        window
            .show()
            .map_err(|_| "Widget window could not be shown.".to_string())?;
        window
            .set_focus()
            .map_err(|_| "Widget window could not be focused.".to_string())?;
        return Ok(());
    }

    let preferences = get_widget_preferences(app.clone())?;
    let window = WebviewWindowBuilder::new(
        &app,
        "planner-widget",
        WebviewUrl::App("index.html?widget=1".into()),
    )
    .title("Personal AI Planner — Today")
    .inner_size(360.0, 430.0)
    .min_inner_size(300.0, 360.0)
    .resizable(true)
    .always_on_top(preferences.always_on_top)
    .build()
    .map_err(|_| "Widget window could not be created.".to_string())?;
    if let (Some(x), Some(y)) = (preferences.x, preferences.y) {
        window
            .set_position(tauri::PhysicalPosition::new(x, y))
            .map_err(|_| "Widget position could not be restored.".to_string())?;
    }
    window
        .set_size(tauri::PhysicalSize::new(
            preferences.width,
            preferences.height,
        ))
        .map_err(|_| "Widget size could not be restored.".to_string())?;
    Ok(())
}

#[tauri::command]
fn open_main_window(app: AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Main application window is unavailable.".to_string())?;
    window
        .show()
        .map_err(|_| "Main application window could not be shown.".to_string())?;
    window
        .set_focus()
        .map_err(|_| "Main application window could not be focused.".to_string())
}

#[tauri::command]
fn notify_planner_data_changed(app: AppHandle) -> Result<(), String> {
    app.emit("planner-data-changed", ())
        .map_err(|_| "Planner windows could not be notified of updated data.".to_string())
}

fn main() {
    let executable_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .unwrap_or_default();
    let backend = resolve_backend_path(&executable_dir);

    let app = tauri::Builder::default()
        .setup(move |app| {
            let data_dir = match app.path().app_data_dir() {
                Ok(path) => path,
                Err(_error) => {
                    #[cfg(debug_assertions)]
                    eprintln!("Could not resolve the application data directory: {_error}");
                    return Ok(());
                }
            };
            if let Err(error) = fs::create_dir_all(&data_dir) {
                write_diagnostic(
                    &data_dir.join("backend.log"),
                    &format!(
                        "Could not create the application data directory ({:?}).",
                        error.kind()
                    ),
                );
                return Ok(());
            }
            let log_path = data_dir.join("backend.log");
            migrate_legacy_data(&data_dir, &executable_dir);

            let process = Arc::new(BackendProcess {
                child: Mutex::new(None),
                backend_path: backend,
                data_dir,
                log_path: log_path.clone(),
                start_lock: Mutex::new(()),
            });
            app.manage(Arc::clone(&process));
            if let Err(error) = process.ensure_running() {
                write_diagnostic(&log_path, &error);
            }
            monitor_backend(process);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_widget_preferences,
            save_widget_preferences,
            open_widget,
            open_main_window,
            notify_planner_data_changed,
            retry_backend
        ])
        .build(tauri::generate_context!())
        .expect("error while building Personal AI Planner");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(process) = app_handle.try_state::<Arc<BackendProcess>>() {
                if let Ok(mut child_slot) = process.child.lock() {
                    if let Some(child) = child_slot.as_mut() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                    *child_slot = None;
                }
                write_diagnostic(&process.log_path, "Desktop application exited.");
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{copy_if_missing, resolve_backend_path};
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_directory() -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("personal-ai-planner-migration-{unique}"))
    }

    #[test]
    fn migration_preserves_existing_data_and_copies_missing_files() {
        let root = temporary_directory();
        let legacy = root.join("legacy");
        let legacy_resources = legacy.join("resources");
        let data = root.join("app-data");
        fs::create_dir_all(&legacy_resources).expect("create legacy resource directory");
        fs::create_dir_all(&data).expect("create app data directory");
        fs::write(legacy_resources.join("tasks.json"), "legacy tasks").expect("write legacy tasks");
        fs::write(legacy_resources.join("token.json"), "legacy token").expect("write legacy token");
        fs::write(data.join("tasks.json"), "newer tasks").expect("write existing app data");

        assert_eq!(
            copy_if_missing(
                &legacy_resources.join("tasks.json"),
                &data.join("tasks.json")
            )
            .expect("skip existing data"),
            false
        );
        assert_eq!(
            copy_if_missing(
                &legacy_resources.join("token.json"),
                &data.join("token.json")
            )
            .expect("copy missing token"),
            true
        );
        assert_eq!(
            copy_if_missing(
                &legacy_resources.join("token.json"),
                &data.join("token.json")
            )
            .expect("do not overwrite migrated token"),
            false
        );

        assert_eq!(
            fs::read_to_string(data.join("tasks.json")).expect("read preserved tasks"),
            "newer tasks"
        );
        assert_eq!(
            fs::read_to_string(data.join("token.json")).expect("read migrated token"),
            "legacy token"
        );
        fs::remove_dir_all(root).expect("remove temporary migration fixture");
    }

    #[test]
    fn packaged_backend_path_uses_tauri_external_binary_name() {
        let root = temporary_directory();
        fs::create_dir_all(&root).expect("create executable directory");
        let packaged = root.join("api.exe");
        fs::write(&packaged, "packaged sidecar").expect("write packaged sidecar");
        assert_eq!(resolve_backend_path(&root), packaged);
        fs::remove_dir_all(root).expect("remove temporary executable directory");
    }
}
