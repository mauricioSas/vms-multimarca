//! `vmshost.exe`: arrancador fijo de los servicios (PLAN-V2 §1.3, CONTRATO §13.3). Dueño: B1.
//!
//! Esqueleto de la fase 0. La prueba de concepto que valida el diseño (servicio con
//! windows-service-rs, puntero `active.json`, Job Object, cuenta virtual y vuelta atrás) está en
//! `spikes/s4-servicio-windows/`; B1 la convierte aquí en el producto.

use vms_common::exit_codes;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--version") => println!("vmshost {}", env!("CARGO_PKG_VERSION")),
        _ => {
            eprintln!("vmshost {}: esqueleto de la fase 0 (sin implementar)", env!("CARGO_PKG_VERSION"));
            std::process::exit(exit_codes::USAGE);
        }
    }
}
