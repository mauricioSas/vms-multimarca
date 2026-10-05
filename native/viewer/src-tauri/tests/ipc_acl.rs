//! Capacidades de Tauri (PLAN-V2 §6.2 B2, criterio 5): las páginas del backend (orígenes remotos, también
//! 127.0.0.1:8600) no pueden llamar a NINGÚN comando IPC; las páginas locales del visor solo a los suyos.
//!
//! Se usa el runtime simulado de Tauri con la configuración y las capacidades reales (`generate_context!`):
//! la comprobación es la misma que hace la aplicación con cada mensaje IPC, según la URL que lo envía.

use std::sync::Arc;

use tauri::ipc::{CallbackFn, InvokeBody};
use tauri::test::{get_ipc_response, mock_builder, MockRuntime, INVOKE_KEY};
use tauri::webview::InvokeRequest;
use tauri::{App, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use vms_viewer::app::{handlers, COMMANDS};
use vms_viewer::state::Viewer;

const LOCAL: &str =
    if cfg!(windows) { "http://tauri.localhost/servidores.html" } else { "tauri://localhost/servidores.html" };

/// Orígenes que nunca deben tener IPC: el backend local, uno remoto y una página cualquiera.
const REMOTE: &[&str] = &[
    "http://127.0.0.1:8600/wall/1",
    "http://127.0.0.1:8600/api/local/kiosk",
    "https://central:8643/",
    "https://evil.example/",
    "http://tauri.localhost.evil.example/",
];

fn app() -> (tempfile::TempDir, App<MockRuntime>, WebviewWindow<MockRuntime>) {
    let dir = tempfile::tempdir().unwrap();
    let (v, _) = Viewer::new("2.0.0-test", dir.path().join("datos"), dir.path().join("viewer.json"), None);
    let app = mock_builder()
        .manage(Arc::new(v))
        .invoke_handler(handlers())
        .build(tauri::generate_context!())
        .expect("app simulada");
    let w = WebviewWindowBuilder::new(&app, "panel", WebviewUrl::App("index.html".into())).build().unwrap();
    (dir, app, w)
}

fn request(cmd: &str, url: &str, body: serde_json::Value) -> InvokeRequest {
    InvokeRequest {
        cmd: cmd.into(),
        callback: CallbackFn(0),
        error: CallbackFn(1),
        url: url.parse().unwrap(),
        body: InvokeBody::Json(body),
        headers: Default::default(),
        invoke_key: INVOKE_KEY.to_string(),
    }
}

#[test]
fn backend_pages_cannot_invoke_any_command() {
    let (_d, _app, w) = app();
    for origin in REMOTE {
        for cmd in COMMANDS {
            let r = get_ipc_response(&w, request(cmd, origin, serde_json::json!({})));
            let err = r.expect_err(&format!("{cmd} desde {origin} tenía que rechazarse"));
            let text = err.to_string();
            assert!(text.contains("not allowed"), "{cmd} desde {origin}: {text}");
        }
        // tampoco los plugins del núcleo (eventos, ventanas…)
        for cmd in ["plugin:event|listen", "plugin:window|close", "plugin:webview|create_webview_window"] {
            assert!(get_ipc_response(&w, request(cmd, origin, serde_json::json!({}))).is_err(), "{cmd} desde {origin}");
        }
    }
}

#[test]
fn local_pages_reach_only_viewer_commands() {
    let (_d, _app, w) = app();
    let r = get_ipc_response(&w, request("servidores", LOCAL, serde_json::json!({}))).expect("servidores desde local");
    let v: serde_json::Value = r.deserialize().unwrap();
    assert_eq!(v["panel"], "local");
    assert_eq!(v["servidores"][0]["url"], "http://127.0.0.1:8600");
    let r = get_ipc_response(&w, request("acerca", LOCAL, serde_json::json!({}))).expect("acerca");
    assert_eq!(r.deserialize::<serde_json::Value>().unwrap()["producto"], "VMS Multimarca");
    // capacidades mínimas: ni siquiera las páginas locales tienen los plugins del núcleo
    for cmd in ["plugin:event|listen", "plugin:window|close", "plugin:webview|create_webview_window"] {
        assert!(get_ipc_response(&w, request(cmd, LOCAL, serde_json::json!({}))).is_err(), "{cmd} local");
    }
    // un comando que no existe
    assert!(get_ipc_response(&w, request("leer_archivo", LOCAL, serde_json::json!({}))).is_err());
}

#[test]
fn commands_build_rs_and_capability_agree() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    // en Windows, git puede dejar el archivo con CRLF
    let build = std::fs::read_to_string(root.join("build.rs")).unwrap().replace("\r\n", "\n");
    for cmd in COMMANDS {
        assert!(build.contains(&format!("\"{cmd}\",")), "falta {cmd} en build.rs");
    }
    assert_eq!(build.matches("\",\n").count(), COMMANDS.len(), "build.rs tiene comandos de más");
    let cap: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(root.join("capabilities/local.json")).unwrap()).unwrap();
    assert!(cap.get("remote").is_none(), "la capacidad no puede abrir IPC a orígenes remotos");
    assert_eq!(cap["local"], true);
    let perms: Vec<String> =
        cap["permissions"].as_array().unwrap().iter().map(|p| p.as_str().unwrap().to_string()).collect();
    let wanted: Vec<String> = COMMANDS.iter().map(|c| format!("allow-{}", c.replace('_', "-"))).collect();
    assert_eq!(perms, wanted, "solo los comandos propios, uno a uno");
    let conf: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(root.join("tauri.conf.json")).unwrap()).unwrap();
    assert!(conf["build"].get("devUrl").is_none(), "un devUrl convertiría ese origen en «local»");
    assert_eq!(conf["app"]["withGlobalTauri"], false);
    assert_eq!(conf["app"]["security"]["capabilities"], serde_json::json!(["local"]));
}
