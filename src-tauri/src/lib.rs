use std::sync::Mutex;
use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandChild;

struct Backend(Mutex<Option<CommandChild>>);

/// The backend's data folder (the same one backend/core/config.py uses).
fn data_dir() -> Result<std::path::PathBuf, String> {
    if cfg!(target_os = "macos") {
        dirs::home_dir().map(|h| h.join("Library/Application Support/langhirev2"))
    } else if cfg!(target_os = "windows") {
        dirs::data_dir().map(|d| d.join("langhirev2"))
            .or_else(|| dirs::home_dir().map(|h| h.join("AppData").join("Roaming").join("langhirev2")))
    } else {
        dirs::home_dir().map(|h| h.join(".config/langhirev2"))
    }
    .ok_or_else(|| "no home dir".to_string())
}

#[tauri::command]
fn get_api_token() -> Result<String, String> {
    let token_path = data_dir()?.join(".api_token");

    // Backend sidecar may take a few seconds to start and write the token file.
    // Retry up to 30 times (30 seconds total) with 1-second intervals.
    for i in 0..30 {
        match std::fs::read_to_string(&token_path) {
            Ok(s) => {
                let trimmed = s.trim().to_string();
                if !trimmed.is_empty() {
                    return Ok(trimmed);
                }
            }
            Err(_) => {}
        }
        if i < 29 {
            std::thread::sleep(std::time::Duration::from_secs(1));
        }
    }

    Err(format!("Token file not found after 30s: {}", token_path.display()))
}

/// Open a tailored-resume PDF. Only a .pdf inside the app's tailored folder:
/// never another path, program or URL, and never through a shell.
#[tauri::command]
fn open_file(path: String) -> Result<(), String> {
    let allowed = data_dir()?
        .join("tailored")
        .canonicalize()
        .map_err(|_| "No tailored resumes yet".to_string())?;
    let file = std::path::Path::new(&path)
        .canonicalize()
        .map_err(|_| "File not found".to_string())?;
    let is_pdf = file
        .extension()
        .map(|e| e.eq_ignore_ascii_case("pdf"))
        .unwrap_or(false);
    if !file.starts_with(&allowed) || !is_pdf || !file.is_file() {
        return Err("Only tailored resume PDFs can be opened".to_string());
    }
    let path = file.to_string_lossy().trim_start_matches(r"\\?\").to_string();
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("Failed to open file: {}", e))?;
    }
    #[cfg(target_os = "windows")]
    {
        // explorer.exe opens the file with its default app; no cmd.exe parsing.
        std::process::Command::new("explorer")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("Failed to open file: {}", e))?;
    }
    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("Failed to open file: {}", e))?;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_log::Builder::new().build())
        .manage(Backend(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![get_api_token, open_file])
        .setup(|app| {
            let shell = app.shell();
            match shell.sidecar("langhirev2-backend") {
                Ok(sidecar) => match sidecar.args(["8743"]).spawn() {
                    Ok((_rx, child)) => {
                        log::info!("Backend sidecar started on port 8743");
                        *app.state::<Backend>().0.lock().unwrap() = Some(child);
                    }
                    Err(e) => log::error!("Failed to spawn backend sidecar: {e}"),
                },
                Err(e) => log::error!("Backend sidecar binary not found: {e}"),
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app, _event| {
            // Sidecar lifecycle is self-managed:
            // - Parent watchdog thread in Python detects when Tauri exits
            // - SIGTERM handler cleans up browser processes
            // We don't kill the sidecar here because Tauri may fire Exit
            // before the sidecar finishes starting (observed on macOS M1)
        });
}
