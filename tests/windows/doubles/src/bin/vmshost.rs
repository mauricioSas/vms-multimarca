//! Doble de `vmshost.exe`: `viewer [--walls]` abre el visor de la versión activa (CONTRATO §13.3) y
//! `--version` se identifica como doble. No hospeda servicios: eso es de B1.

use std::path::PathBuf;
use vms_dobles::{data_dir, exit_codes, real_env, viewer_path};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        Some("--version") => {
            println!("vmshost-doble {}", env!("CARGO_PKG_VERSION"));
            exit_codes::OK
        }
        Some("viewer") => open_viewer(args.iter().any(|a| a == "--walls")),
        Some("service") => {
            eprintln!("El doble de vmshost no hospeda servicios: hace falta el vmshost de B1.");
            exit_codes::USAGE
        }
        _ => {
            eprintln!("uso: vmshost.exe viewer [--walls] | --version");
            exit_codes::USAGE
        }
    };
    std::process::exit(code);
}

fn open_viewer(walls: bool) -> i32 {
    // <instalación>\bin\vmshost.exe → <instalación>
    let install_root = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().and_then(|b| b.parent()).map(PathBuf::from))
        .unwrap_or_default();
    let exe = match viewer_path(&install_root, &data_dir(&real_env, None)) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("{e}");
            return exit_codes::WINDOWS_ERROR;
        }
    };
    let mut cmd = std::process::Command::new(exe);
    if walls {
        cmd.arg("--walls");
    }
    match cmd.spawn() {
        Ok(_) => exit_codes::OK,
        Err(e) => {
            eprintln!("No se pudo abrir el visor: {e}");
            exit_codes::WINDOWS_ERROR
        }
    }
}
