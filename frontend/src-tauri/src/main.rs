#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let executable_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(|parent| parent.to_path_buf()))
        .unwrap_or_default();
    let backend = executable_dir.join("api-x86_64-pc-windows-msvc.exe");
    let resource_dir = executable_dir.join("resources");
    let data_dir = if resource_dir.join("tasks.json").exists() { resource_dir } else { executable_dir.clone() };
    let mut child = None;

    if backend.exists() {
        child = std::process::Command::new(backend)
            .env("PERSONAL_AI_AGENT_DATA_DIR", &data_dir)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .ok();
    }

    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Personal AI Agent");

    drop(child);
}
