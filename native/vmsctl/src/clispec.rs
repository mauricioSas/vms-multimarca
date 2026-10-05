//! Tabla de la CLI (`cli_spec.json`, CONTRATO §14.1): qué órdenes existen y qué opciones admite cada una.
//!
//! Es la fuente única: `vmsctl` rechaza (código 2) cualquier orden, opción o argumento que no esté aquí,
//! `vmsctl help --json` la devuelve, el doble de pruebas del instalador (`tests/windows/doubles`) la incluye tal
//! cual (así no puede aceptar nada que el real rechace) y `tests/windows/test_vmsctl_cli.py` comprueba con ella
//! cada llamada del instalador.

use crate::cli::{Args, CtlError};
use serde::{Deserialize, Serialize};
use std::sync::OnceLock;

pub const SPEC_JSON: &str = include_str!("cli_spec.json");

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct CommandSpec {
    pub path: Vec<String>,
    #[serde(default)]
    pub values: Vec<String>,
    #[serde(default)]
    pub switches: Vec<String>,
    #[serde(default)]
    pub required: Vec<String>,
    #[serde(default)]
    pub positional: usize,
}

impl CommandSpec {
    pub fn name(&self) -> String {
        self.path.join(" ")
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct Global {
    #[serde(default)]
    pub values: Vec<String>,
    #[serde(default)]
    pub switches: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Spec {
    pub version: u32,
    pub global: Global,
    pub commands: Vec<CommandSpec>,
}

/// La tabla, leída una vez (`cli_spec.json` va dentro del ejecutable; una prueba comprueba que es válida).
pub fn spec() -> &'static Spec {
    static SPEC: OnceLock<Spec> = OnceLock::new();
    SPEC.get_or_init(|| match serde_json::from_str(SPEC_JSON) {
        Ok(s) => s,
        Err(e) => panic!("cli_spec.json no es válido: {e}"),
    })
}

/// ¿`--flag` lleva valor en alguna orden? (lo usa `Args::parse`).
pub fn takes_value(flag: &str) -> bool {
    let s = spec();
    s.global.values.iter().any(|v| v == flag) || s.commands.iter().any(|c| c.values.iter().any(|v| v == flag))
}

/// La orden de `words`: la de ruta más larga que encaje al principio.
pub fn find(words: &[String]) -> Option<&'static CommandSpec> {
    spec()
        .commands
        .iter()
        .filter(|c| c.path.len() <= words.len() && c.path.iter().zip(words).all(|(p, w)| p == w))
        .max_by_key(|c| c.path.len())
}

/// Comprueba la línea de órdenes contra la tabla. Error de uso (2) si algo no encaja.
pub fn validate(a: &Args) -> Result<&'static CommandSpec, CtlError> {
    let s = spec();
    let unknown = || CtlError::usage(format!("orden desconocida «{}». Usa «vmsctl help».", a.words.join(" ")));
    let cmd = find(&a.words).ok_or_else(unknown)?;
    let extra = a.words.len() - cmd.path.len();
    if extra != cmd.positional {
        if cmd.positional == 0 && cmd.path.len() < 2 && cmd.path.first().map(String::as_str) != Some("help") {
            // «services frobnicate», «version x.y» sin «switch»…: es una orden que no existe
            return Err(unknown());
        }
        return Err(CtlError::usage(format!(
            "«{}» espera {} argumento(s) y recibió {}. Usa «vmsctl help».",
            cmd.name(),
            cmd.positional,
            extra
        )));
    }
    for sw in a.switch_names() {
        if !s.global.switches.iter().chain(&cmd.switches).any(|k| k == sw) {
            return Err(CtlError::usage(format!("opción desconocida para «{}»: {sw}. Usa «vmsctl help».", cmd.name())));
        }
    }
    for v in a.value_names() {
        if !s.global.values.iter().chain(&cmd.values).any(|k| k == v) {
            return Err(CtlError::usage(format!("opción desconocida para «{}»: {v}. Usa «vmsctl help».", cmd.name())));
        }
    }
    for r in &cmd.required {
        if a.value(r).is_none() {
            return Err(CtlError::usage(format!("«{}» necesita {r} <valor>", cmd.name())));
        }
    }
    Ok(cmd)
}

#[cfg(test)]
mod tests {
    use super::*;
    use vms_common::exit_codes;

    fn check(line: &str) -> Result<String, CtlError> {
        let raw: Vec<String> = line.split_whitespace().map(str::to_string).collect();
        let a = Args::parse(&raw)?;
        validate(&a).map(CommandSpec::name)
    }

    #[test]
    fn the_spec_is_valid_and_consistent() {
        let s = spec();
        assert_eq!(s.version, 1);
        let mut names: Vec<String> = s.commands.iter().map(CommandSpec::name).collect();
        let n = names.len();
        names.sort();
        names.dedup();
        assert_eq!(names.len(), n, "órdenes repetidas en cli_spec.json");
        for c in &s.commands {
            for f in c.values.iter().chain(&c.switches).chain(&c.required) {
                assert!(f.starts_with("--"), "{}: {f}", c.name());
            }
            for r in &c.required {
                assert!(c.values.contains(r), "{}: {r} obligatoria pero no admitida", c.name());
            }
            for sw in &c.switches {
                assert!(!takes_value(sw), "{sw} es a la vez interruptor y opción con valor");
            }
        }
    }

    #[test]
    fn every_command_is_in_the_help_text() {
        for c in &spec().commands {
            if c.path.is_empty() {
                continue;
            }
            let first = &c.path[0];
            assert!(crate::HELP.contains(first.as_str()), "falta «{first}» en la ayuda");
            if let Some(sub) = c.path.get(1) {
                assert!(crate::HELP.contains(sub.as_str()), "falta «{}» en la ayuda", c.name());
            }
        }
    }

    #[test]
    fn accepts_what_the_installer_and_the_updater_send() {
        for line in [
            "ports check --role store --http-port 8600 --https-port 8643 --data-dir C:\\datos --json",
            "services install --role store --data-dir C:\\datos --recordings-dir D:\\Grabaciones --json",
            "acl apply --role store --recordings-dir D:\\Grabaciones --json",
            "update lock --owner installer --ttl 3600 --json",
            "update unlock --owner installer --json",
            "services start --only VMSBackend,VMSEngine --json",
            "health wait --timeout 120 --deep --json",
            "run --service VMSBackend --data-dir C:\\datos --stop-on-stdin-eof --exit-on-crash",
            "tls setup --hostname tienda --ip 10.0.0.2 --ip 10.0.0.3 --import-root",
            "version switch 2.1.0",
            "version",
            "help --json",
        ] {
            check(line).unwrap_or_else(|e| panic!("«{line}» rechazada: {}", e.message));
        }
        assert_eq!(check("").unwrap(), "");
    }

    #[test]
    fn rejects_what_it_does_not_know() {
        for (line, why) in [
            ("ports check --http-port", "falta el valor"),
            ("acl apply --profiles private", "opción desconocida"),
            ("ports check --role store --purge", "opción desconocida"),
            ("update lock --ttl 5", "necesita --owner"),
            ("version switch", "argumento"),
            ("version switch 2.0.0 2.0.1", "argumento"),
            ("services frobnicate", "orden desconocida"),
            ("frobnicate", "orden desconocida"),
            ("version frobnicate", "orden desconocida"),
            ("diag bundle", "necesita --out"),
        ] {
            let e = check(line).err().unwrap_or_else(|| panic!("«{line}» tendría que fallar"));
            assert_eq!(e.exit, exit_codes::USAGE, "{line}");
            assert!(e.message.contains(why), "«{line}»: {}", e.message);
        }
    }
}
