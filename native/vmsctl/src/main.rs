//! `vmsctl.exe`: anfitrión de procesos y configuración del equipo (PLAN-V2 §1.3, CONTRATO §14).
//! Dueño: B1. Esqueleto de la fase 0: solo responde `--version`.

use vms_common::exit_codes;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--version") => println!("vmsctl {}", env!("CARGO_PKG_VERSION")),
        _ => {
            eprintln!("vmsctl {}: esqueleto de la fase 0 (sin implementar)", env!("CARGO_PKG_VERSION"));
            std::process::exit(exit_codes::USAGE);
        }
    }
}
