//! Visor de escritorio de VMS Multimarca (`VMS.exe`, Tauri 2.12.1). PLAN-V2 §1.1, §2.3 y §2.5; CONTRATO §17.
//!
//! El visor no contiene el backend: muestra la interfaz que sirve el servicio (`/`, `/wall/N`, `/status`…) y
//! solo tiene páginas propias para lo que pasa antes de conectar (`ui/`). Las páginas del backend no tienen
//! acceso a IPC.

pub mod app;
pub mod commands;
pub mod config;
pub mod github_update;
pub mod health;
pub mod http;
pub mod kiosk;
pub mod log;
pub mod monitors;
pub mod paths;
pub mod pinning;
pub mod platform;
pub mod servers;
pub mod state;
pub mod tray_icons;
pub mod updates;
#[cfg(windows)]
mod webview2_cert;

pub use app::run;
