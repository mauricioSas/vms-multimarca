//! Visor mínimo de la prueba S1: abre N ventanas (una por monitor si hay bastantes; si no, en
//! cuadrícula sobre el principal para que ninguna quede tapada) y cada una reproduce M flujos WHEP
//! de MediaMTX. Las páginas (`dist/s1.html`) envían sus estadísticas por IPC (`report`) y aquí se
//! escriben, una línea JSON por medida, en el archivo de `--out`.
//!
//!     s1-visor.exe --windows 4 --cells 16 --base http://127.0.0.1:8889 --prefix h264- --minutes 30 --out stats.jsonl
//!
//! Todas las ventanas comparten la carpeta de datos de WebView2 (un solo proceso de GPU), como hará el
//! visor de la v2 (PLAN-V2 §2.3).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::{Manager, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindowBuilder};

struct Out(Mutex<PathBuf>);

#[tauri::command]
fn report(state: tauri::State<'_, Out>, line: String) -> Result<(), String> {
    let path = state.0.lock().map_err(|e| e.to_string())?.clone();
    let mut f = OpenOptions::new().create(true).append(true).open(&path).map_err(|e| e.to_string())?;
    writeln!(f, "{line}").map_err(|e| e.to_string())
}

#[tauri::command]
fn quit(app: tauri::AppHandle) {
    app.exit(0);
}

fn arg(name: &str) -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned())
}

fn encode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn main() {
    let windows: u32 = arg("--windows").and_then(|v| v.parse().ok()).unwrap_or(4).clamp(1, 8);
    let cells: u32 = arg("--cells").and_then(|v| v.parse().ok()).unwrap_or(16).clamp(1, 16);
    let base = arg("--base").unwrap_or_else(|| "http://127.0.0.1:8889".into());
    let prefix = arg("--prefix").unwrap_or_else(|| "h264-".into());
    let minutes = arg("--minutes").unwrap_or_else(|| "30".into());
    let page = arg("--page").unwrap_or_else(|| "s1.html".into());
    let extra = arg("--query").unwrap_or_default();
    let out = arg("--out").map(PathBuf::from).unwrap_or_else(|| std::env::temp_dir().join("vms-s1-stats.jsonl"));

    tauri::Builder::default()
        .manage(Out(Mutex::new(out)))
        .invoke_handler(tauri::generate_handler![report, quit])
        .setup(move |app| {
            let monitors = app.available_monitors()?;
            let primary = app.primary_monitor()?.or_else(|| monitors.first().cloned());
            for i in 0..windows {
                let url = format!(
                    "{page}?w={}&windows={windows}&cells={cells}&base={}&prefix={}&minutes={}{}",
                    i + 1,
                    encode(&base),
                    encode(&prefix),
                    encode(&minutes),
                    if extra.is_empty() { String::new() } else { format!("&{extra}") }
                );
                let w = WebviewWindowBuilder::new(app, format!("muro{}", i + 1), WebviewUrl::App(url.into()))
                    .title(format!("S1 · muro {}", i + 1))
                    .build()?;
                if monitors.len() >= windows as usize {
                    let m = &monitors[i as usize];
                    w.set_position(*m.position())?;
                    w.set_size(*m.size())?;
                } else if let Some(m) = &primary {
                    // Cuadrícula de 2 columnas sobre el monitor principal: ninguna ventana tapa a otra
                    // (una ventana tapada podría decodificar menos y daría un resultado optimista).
                    let rows = windows.div_ceil(2);
                    let (cw, ch) = (m.size().width / 2, m.size().height / rows);
                    w.set_size(PhysicalSize::new(cw, ch))?;
                    w.set_position(PhysicalPosition::new(
                        m.position().x + ((i % 2) * cw) as i32,
                        m.position().y + ((i / 2) * ch) as i32,
                    ))?;
                }
            }
            if let Some(w) = app.get_webview_window("muro1") {
                let _ = w.set_focus();
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("no se pudo arrancar el visor de S1");
}
