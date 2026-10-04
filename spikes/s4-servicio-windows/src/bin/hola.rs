//! Proceso hijo de la prueba S4 («el backend»). Escribe un latido con su versión cada segundo.
//! Si en su carpeta de versión existe `CRASH`, termina con error al poco de arrancar (versión rota).
//!
//!     hola --version 1.0.0 --root C:\vms-s4

use std::path::PathBuf;
use std::time::Duration;

fn arg(name: &str) -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned())
}

fn main() {
    let version = arg("--version").unwrap_or_else(|| "?".into());
    let root = PathBuf::from(arg("--root").unwrap_or_else(|| ".".into()));
    let crash = root.join("versions").join(&version).join("CRASH").exists();
    // ¿La cuenta del servicio puede escribir donde no debe? (bin\ es solo lectura para ella)
    let probe = root.join("bin").join(format!("probe-{}.txt", std::process::id()));
    let can_write_bin = std::fs::write(&probe, b"x").is_ok();
    let _ = std::fs::remove_file(&probe);
    println!("hola {version}: pid {} (crash={crash}, can_write_bin={can_write_bin})", std::process::id());
    if crash {
        std::thread::sleep(Duration::from_millis(300));
        eprintln!("hola {version}: versión rota a propósito, salgo con código 3");
        std::process::exit(3);
    }
    let hb = root.join("state").join("heartbeat.json");
    loop {
        let body = serde_json::json!({
            "version": version, "pid": std::process::id(), "ts": s4_servicio_windows::now_s(),
            "can_write_bin": can_write_bin,
        });
        if let Err(e) = vms_common::atomic_write(&hb, body.to_string().as_bytes()) {
            eprintln!("hola: no se pudo escribir el latido: {e}");
        }
        std::thread::sleep(Duration::from_secs(1));
    }
}
