//! Ejecución de herramientas del sistema (`icacls`, `netsh`, `certutil`, `python -m vms …`) detrás de un
//! rasgo, para probar el orden y los argumentos exactos sin Windows (dobles en memoria, PLAN-V2 §4.5).
//!
//! Solo se mira el **código de salida**: nunca se analiza el texto (que Windows traduce al idioma del
//! equipo).

use std::ffi::OsString;
use std::path::Path;
use std::process::{Command, Stdio};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Exec {
    pub code: i32,
    /// Últimas líneas de la salida de error (para el mensaje al usuario; nunca se interpretan).
    pub stderr_tail: String,
}

impl Exec {
    pub fn ok(&self) -> bool {
        self.code == 0
    }
}

pub trait Runner {
    fn run(&mut self, program: &Path, args: &[OsString], env: &[(String, OsString)]) -> std::io::Result<Exec>;
}

pub struct RealRunner;

impl Runner for RealRunner {
    fn run(&mut self, program: &Path, args: &[OsString], env: &[(String, OsString)]) -> std::io::Result<Exec> {
        let mut cmd = Command::new(program);
        cmd.args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped());
        for (k, v) in env {
            cmd.env(k, v);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
        }
        let out = cmd.output()?;
        let err = String::from_utf8_lossy(&out.stderr);
        let tail: Vec<&str> = err.lines().rev().take(5).collect();
        let tail = tail.into_iter().rev().collect::<Vec<_>>().join(" | ");
        Ok(Exec { code: out.status.code().unwrap_or(-1), stderr_tail: vms_common::redact(&tail).into_owned() })
    }
}

/// Ruta de una herramienta de `System32` (no se busca en el PATH: nadie puede colar otra `icacls.exe`).
pub fn system32(tool: &str) -> std::path::PathBuf {
    let root = std::env::var_os("SystemRoot").map(std::path::PathBuf::from).unwrap_or_else(|| r"C:\Windows".into());
    root.join("System32").join(tool)
}

#[cfg(test)]
pub mod fake {
    use super::*;

    /// Registra cada orden y devuelve el código que se le diga (por subcadena de la línea de órdenes).
    #[derive(Default)]
    pub struct FakeRunner {
        pub calls: Vec<String>,
        pub codes: Vec<(String, i32)>,
    }

    impl Runner for FakeRunner {
        fn run(&mut self, program: &Path, args: &[OsString], _env: &[(String, OsString)]) -> std::io::Result<Exec> {
            let name = program.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let line = std::iter::once(name)
                .chain(args.iter().map(|a| a.to_string_lossy().into_owned()))
                .collect::<Vec<_>>()
                .join(" ");
            let code = self.codes.iter().find(|(k, _)| line.contains(k.as_str())).map(|(_, c)| *c).unwrap_or(0);
            self.calls.push(line);
            Ok(Exec { code, stderr_tail: String::new() })
        }
    }
}
