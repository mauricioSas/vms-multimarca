//! Doble de `vmsctl.exe` (ver `lib.rs`). Registra cada llamada y responde como dice CONTRATO §14.

use std::io::Write;
use vms_dobles::{calls_log, real_env, record_call, run_vmsctl, Effects};

#[cfg(not(windows))]
struct Portable;

#[cfg(not(windows))]
impl Effects for Portable {
    fn elevated(&self) -> bool {
        false
    }
    fn user(&self) -> String {
        std::env::var("USER").unwrap_or_default()
    }
    fn remove_v1_services(&mut self) -> Result<Vec<String>, String> {
        Ok(Vec::new())
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    #[cfg(windows)]
    let mut fx = vms_dobles::win::System;
    #[cfg(not(windows))]
    let mut fx = Portable;
    let out = run_vmsctl(&args, &real_env, &mut fx);
    let mut code = out.code;
    if args.first().map(String::as_str) != Some("--version") {
        if let Err(e) = record_call(&calls_log(&real_env), &args, out.code, &fx as &dyn Effects) {
            eprintln!("doble de vmsctl: {e}");
            code = vms_dobles::exit_codes::WINDOWS_ERROR;
        }
    }
    let _ = std::io::stdout().write_all(out.stdout.as_bytes());
    let _ = std::io::stderr().write_all(out.stderr.as_bytes());
    std::process::exit(code);
}
