//! Argumentos, errores y salida de `vmsctl` (CONTRATO §14.2).
//!
//! Con `--json`, una sola línea UTF-8:
//! `{"ok": false, "code": 10, "data": {…}, "error": {"code": "port_in_use", "message_es": "…", "win32": null}}`.
//! Sin `--json`, un resumen legible en la salida estándar y los errores en la de error.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::fmt;
use std::io;
use vms_common::exit_codes;
use vms_common::state::StateError;

/// Opciones que llevan valor (el resto son interruptores).
const VALUE_FLAGS: &[&str] = &[
    "--service",
    "--role",
    "--data-dir",
    "--install-dir",
    "--only",
    "--profiles",
    "--timeout",
    "--to",
    "--reason",
    "--hostname",
    "--ip",
    "--out",
    "--python",
    "--mediamtx",
    "--recordings-dir",
    "--ports",
];

#[derive(Debug, Default)]
pub struct Args {
    pub words: Vec<String>,
    values: HashMap<String, Vec<String>>,
    switches: Vec<String>,
}

impl Args {
    pub fn parse(raw: &[String]) -> Result<Args, CtlError> {
        let mut a = Args::default();
        let mut it = raw.iter().peekable();
        while let Some(tok) = it.next() {
            if tok == "--" {
                a.words.extend(it.by_ref().cloned());
                break;
            }
            if let Some((k, v)) = tok.split_once('=').filter(|(k, _)| k.starts_with("--")) {
                a.values.entry(k.to_string()).or_default().push(v.to_string());
            } else if VALUE_FLAGS.contains(&tok.as_str()) {
                let v = it.next().ok_or_else(|| CtlError::usage(format!("falta el valor de {tok}")))?;
                a.values.entry(tok.clone()).or_default().push(v.clone());
            } else if tok.starts_with("--") {
                a.switches.push(tok.clone());
            } else {
                a.words.push(tok.clone());
            }
        }
        Ok(a)
    }

    pub fn value(&self, name: &str) -> Option<&str> {
        self.values.get(name).and_then(|v| v.last()).map(String::as_str)
    }

    pub fn values(&self, name: &str) -> Vec<String> {
        self.values.get(name).cloned().unwrap_or_default()
    }

    pub fn has(&self, name: &str) -> bool {
        self.switches.iter().any(|s| s == name)
    }

    pub fn word(&self, i: usize) -> Option<&str> {
        self.words.get(i).map(String::as_str)
    }

    /// Interruptores que nadie ha consultado (para avisar de errores de tecleo).
    pub fn unknown_switches(&self, known: &[&str]) -> Vec<String> {
        self.switches.iter().filter(|s| !known.contains(&s.as_str())).cloned().collect()
    }
}

#[derive(Debug)]
pub struct CtlError {
    pub exit: i32,
    pub code: &'static str,
    pub message: String,
    pub win32: Option<i32>,
    pub data: Option<Value>,
}

impl CtlError {
    pub fn new(exit: i32, code: &'static str, message: impl Into<String>) -> Self {
        Self { exit, code, message: message.into(), win32: None, data: None }
    }
    pub fn usage(message: impl Into<String>) -> Self {
        Self::new(exit_codes::USAGE, "usage", message)
    }
    pub fn permission(message: impl Into<String>) -> Self {
        Self::new(exit_codes::NO_PERMISSION, "no_permission", message)
    }
    pub fn health(message: impl Into<String>) -> Self {
        Self::new(exit_codes::HEALTH_FAILED, "health_failed", message)
    }
    pub fn windows(message: impl Into<String>) -> Self {
        Self::new(exit_codes::WINDOWS_ERROR, "windows_error", message)
    }
    pub fn with_data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }

    /// Error de E/S con contexto: permiso denegado → 11; el resto → 20 con el código Win32.
    pub fn io(e: &io::Error, context: &str) -> Self {
        if e.kind() == io::ErrorKind::PermissionDenied {
            let mut err = Self::permission(format!(
                "{context}: permiso denegado. Abre la consola como Administrador (elevada) y repite la orden."
            ));
            err.win32 = e.raw_os_error();
            return err;
        }
        let mut err = Self::windows(format!("{context}: {e}"));
        if cfg!(windows) {
            err.win32 = e.raw_os_error();
        }
        err
    }
}

impl fmt::Display for CtlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl From<StateError> for CtlError {
    fn from(e: StateError) -> Self {
        match e {
            StateError::Io(ref p, ref io) => CtlError::io(io, &p.display().to_string()),
            StateError::Invalid(_) => CtlError::usage(e.to_string()),
            _ => CtlError::windows(e.to_string()),
        }
    }
}

/// Resultado correcto: datos para `--json` y texto para personas.
#[derive(Debug)]
pub struct Outcome {
    pub data: Value,
    pub text: String,
}

impl Outcome {
    pub fn new(data: Value, text: impl Into<String>) -> Self {
        Self { data, text: text.into() }
    }
}

pub type CtlResult = Result<Outcome, CtlError>;

/// Escribe el resultado y devuelve el código de salida.
pub fn emit(json_mode: bool, result: CtlResult) -> i32 {
    match result {
        Ok(out) => {
            if json_mode {
                println!("{}", json!({"ok": true, "code": 0, "data": out.data, "error": null}));
            } else if !out.text.is_empty() {
                println!("{}", out.text);
            }
            exit_codes::OK
        }
        Err(e) => {
            if json_mode {
                println!(
                    "{}",
                    json!({"ok": false, "code": e.exit, "data": e.data.clone().unwrap_or(Value::Null),
                           "error": {"code": e.code, "message_es": e.message, "win32": e.win32}})
                );
            } else {
                eprintln!("vmsctl: {}", e.message);
                if let Some(code) = e.win32 {
                    eprintln!("vmsctl: código de Windows {code}");
                }
            }
            e.exit
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(s: &str) -> Args {
        Args::parse(&s.split_whitespace().map(str::to_string).collect::<Vec<_>>()).unwrap()
    }

    #[test]
    fn parses_words_values_and_switches() {
        let a = args("services install --role store --data-dir=D:\\datos --json --only VMSEngine,VMSBackend");
        assert_eq!(a.words, ["services", "install"]);
        assert_eq!(a.value("--role"), Some("store"));
        assert_eq!(a.value("--data-dir"), Some("D:\\datos"));
        assert_eq!(a.value("--only"), Some("VMSEngine,VMSBackend"));
        assert!(a.has("--json") && !a.has("--purge"));
        assert_eq!(a.unknown_switches(&["--json"]), Vec::<String>::new());
        assert!(Args::parse(&["--role".to_string()]).is_err());
        let a = args("tls setup --ip 10.0.0.2 --ip 192.168.1.2");
        assert_eq!(a.values("--ip"), ["10.0.0.2", "192.168.1.2"]);
    }

    #[test]
    fn io_errors_map_to_stable_codes() {
        let e = CtlError::io(&io::Error::from(io::ErrorKind::PermissionDenied), "state\\active.json");
        assert_eq!((e.exit, e.code), (exit_codes::NO_PERMISSION, "no_permission"));
        assert!(e.message.contains("Administrador"));
        let e = CtlError::io(&io::Error::other("x"), "y");
        assert_eq!(e.exit, exit_codes::WINDOWS_ERROR);
    }
}
