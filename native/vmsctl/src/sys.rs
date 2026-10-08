//! Ejecución de herramientas del sistema (`icacls`, `netsh`, `certutil`, `python -m vms …`) detrás de un
//! rasgo, para probar el orden y los argumentos exactos sin Windows (dobles en memoria, PLAN-V2 §4.5).
//!
//! Por el mismo rasgo pasan las dos llamadas de Win32 que cambian el equipo sin herramienta: el grupo local
//! «VMS Operadores» (`NetLocalGroupAdd`) y la clave `HKLM\SOFTWARE\VMSMultimarca`. Así las pruebas nunca tocan
//! el equipo de verdad: antes, `cargo test` sin administrador fallaba con «código 5» y, en una consola de
//! administrador de un PC con el producto instalado, creaba el grupo y escribía y borraba la clave real.
//!
//! Solo se mira el **código de salida**: nunca se analiza el texto (que Windows traduce al idioma del
//! equipo).

use crate::cli::CtlError;
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
    /// Crea el grupo local «VMS Operadores» si no existe (true si lo ha creado).
    fn ensure_operators_group(&mut self) -> Result<bool, CtlError>;
    /// Valor de `HKLM\SOFTWARE\VMSMultimarca` (None si no está; fuera de Windows, siempre None).
    fn product_value(&self, name: &str) -> Option<String>;
    /// Escribe valores de texto en `HKLM\SOFTWARE\VMSMultimarca` (fuera de Windows no hace nada).
    fn set_product_values(&mut self, values: &[(&str, String)]) -> std::io::Result<()>;
    /// Borra `HKLM\SOFTWARE\VMSMultimarca` entera (no es error si no existe).
    fn delete_product_key(&mut self) -> std::io::Result<()>;
}

pub struct RealRunner;

impl Runner for RealRunner {
    fn ensure_operators_group(&mut self) -> Result<bool, CtlError> {
        crate::acl::ensure_operators_group()
    }

    fn product_value(&self, name: &str) -> Option<String> {
        #[cfg(windows)]
        {
            crate::winreg::get_string(crate::winreg::PRODUCT_KEY, name).ok().flatten()
        }
        #[cfg(not(windows))]
        {
            let _ = name;
            None
        }
    }

    fn set_product_values(&mut self, values: &[(&str, String)]) -> std::io::Result<()> {
        #[cfg(windows)]
        for (k, v) in values {
            crate::winreg::set_string(crate::winreg::PRODUCT_KEY, k, v)?;
        }
        #[cfg(not(windows))]
        let _ = values;
        Ok(())
    }

    fn delete_product_key(&mut self) -> std::io::Result<()> {
        #[cfg(windows)]
        {
            crate::winreg::delete_tree(crate::winreg::PRODUCT_KEY)
        }
        #[cfg(not(windows))]
        {
            Ok(())
        }
    }

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

    /// Registra cada orden y devuelve el código que se le diga (por subcadena de la línea de órdenes). El grupo
    /// local y la clave del producto quedan en memoria (`group`, `registry`) y también en `calls`, en orden.
    #[derive(Default)]
    pub struct FakeRunner {
        pub calls: Vec<String>,
        pub codes: Vec<(String, i32)>,
        pub group: bool,
        pub registry: std::collections::BTreeMap<String, String>,
    }

    pub const GROUP_CALL: &str = "NetLocalGroupAdd VMS Operadores";
    pub const PRODUCT_KEY: &str = "HKLM/SOFTWARE/VMSMultimarca";

    impl Runner for FakeRunner {
        fn ensure_operators_group(&mut self) -> Result<bool, CtlError> {
            self.calls.push(GROUP_CALL.to_string());
            let created = !self.group;
            self.group = true;
            Ok(created)
        }

        fn product_value(&self, name: &str) -> Option<String> {
            self.registry.get(name).cloned()
        }

        fn set_product_values(&mut self, values: &[(&str, String)]) -> std::io::Result<()> {
            let names: Vec<&str> = values.iter().map(|(k, _)| *k).collect();
            self.calls.push(format!("reg set {PRODUCT_KEY} {}", names.join(" ")));
            for (k, v) in values {
                self.registry.insert(k.to_string(), v.clone());
            }
            Ok(())
        }

        fn delete_product_key(&mut self) -> std::io::Result<()> {
            self.calls.push(format!("reg delete {PRODUCT_KEY}"));
            self.registry.clear();
            Ok(())
        }

        fn run(&mut self, program: &Path, args: &[OsString], _env: &[(String, OsString)]) -> std::io::Result<Exec> {
            let name = program.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let line = std::iter::once(name)
                .chain(args.iter().map(|a| a.to_string_lossy().into_owned()))
                .collect::<Vec<_>>()
                .join(" ")
                .replace('\\', "/"); // misma forma en Windows y en POSIX
            let code = self.codes.iter().find(|(k, _)| line.contains(k.as_str())).map(|(_, c)| *c).unwrap_or(0);
            self.calls.push(line);
            Ok(Exec { code, stderr_tail: String::new() })
        }
    }
}
