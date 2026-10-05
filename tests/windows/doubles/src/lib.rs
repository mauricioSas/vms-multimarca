//! Dobles de prueba del instalador (B3, PLAN-V2 §6.2).
//!
//! - `vmsctl.exe` acepta **exactamente** la CLI de CONTRATO §14 (más las órdenes que B3 pide a B1 en su
//!   informe: `update lock|unlock`, `ports check --http-port/--https-port` y `--data-dir` global), registra
//!   cada llamada en un JSONL y responde como el real (`--json`, códigos de §14.2). Fallos a petición con
//!   `VMS_FAKE_VMSCTL_FAIL="health-wait=12,ports-check=10"`. Efectos reales mínimos para que el e2e sea
//!   creíble: `version switch` escribe `state\active.json` (§13.4) de forma atómica, `diag bundle` crea un
//!   zip válido y `migrate-from-v1` para y elimina los servicios WinSW de la v1.
//! - `vmshost.exe` solo sabe `viewer [--walls]` (abre el visor de la versión activa) y `--version`.
//! - `VMS.exe` abre una ventana y nada más.
//!
//! La lógica vive aquí (probada con `cargo test` en cualquier sistema); lo que toca Windows, en `win`.

use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(windows)]
pub mod win;

/// Códigos de salida de CONTRATO §14.2 (los mismos que `vms_common::exit_codes`).
pub mod exit_codes {
    pub const OK: i32 = 0;
    pub const USAGE: i32 = 2;
    pub const PORT_IN_USE: i32 = 10;
    pub const NO_PERMISSION: i32 = 11;
    pub const HEALTH_FAILED: i32 = 12;
    pub const WINDOWS_ERROR: i32 = 20;
}

pub const DOUBLE_VERSION: &str = concat!("vmsctl-doble ", env!("CARGO_PKG_VERSION"));
pub const ROLES: [&str; 4] = ["control", "store", "central", "viewer"];

/// Lectura del entorno inyectable (las pruebas no tocan el entorno real del proceso).
pub type Env<'a> = &'a dyn Fn(&str) -> Option<String>;

pub fn real_env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn program_data(env: Env) -> PathBuf {
    PathBuf::from(env("ProgramData").or_else(|| env("PROGRAMDATA")).unwrap_or_else(|| r"C:\ProgramData".into()))
}

/// Carpeta de datos: `--data-dir`, `VMS_DATA_DIR` o `%ProgramData%\VMSMultimarca` (CONTRATO §13.1).
pub fn data_dir(env: Env, flag: Option<&str>) -> PathBuf {
    if let Some(f) = flag {
        return PathBuf::from(f);
    }
    env("VMS_DATA_DIR").map(PathBuf::from).unwrap_or_else(|| program_data(env).join("VMSMultimarca"))
}

/// Registro de llamadas: `VMS_FAKE_VMSCTL_LOG` o `%ProgramData%\VMSMultimarca-pruebas\vmsctl-calls.jsonl`.
/// Fuera de la carpeta de datos del producto: así una desinstalación con `/PURGE` no lo borra.
pub fn calls_log(env: Env) -> PathBuf {
    env("VMS_FAKE_VMSCTL_LOG")
        .map(PathBuf::from)
        .unwrap_or_else(|| program_data(env).join("VMSMultimarca-pruebas").join("vmsctl-calls.jsonl"))
}

pub fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

// ================================================================================================ CLI
struct Spec {
    path: &'static [&'static str],
    flags: &'static [&'static str],
    required: &'static [&'static str],
    switches: &'static [&'static str],
    positional: usize,
}

const SPECS: &[Spec] = &[
    Spec { path: &["run"], flags: &["service"], required: &["service"], switches: &[], positional: 0 },
    Spec {
        path: &["services", "install"],
        flags: &["role", "data-dir"],
        required: &["role", "data-dir"],
        switches: &[],
        positional: 0,
    },
    Spec { path: &["services", "uninstall"], flags: &[], required: &[], switches: &["purge"], positional: 0 },
    Spec { path: &["services", "start"], flags: &["only"], required: &[], switches: &[], positional: 0 },
    Spec { path: &["services", "stop"], flags: &["only"], required: &[], switches: &[], positional: 0 },
    Spec { path: &["services", "restart"], flags: &["only"], required: &[], switches: &[], positional: 0 },
    Spec { path: &["firewall", "apply"], flags: &["profiles"], required: &["profiles"], switches: &[], positional: 0 },
    Spec { path: &["firewall", "remove"], flags: &[], required: &[], switches: &[], positional: 0 },
    Spec { path: &["acl", "apply"], flags: &["data-dir"], required: &["data-dir"], switches: &[], positional: 0 },
    Spec {
        path: &["ports", "check"],
        flags: &["http-port", "https-port"],
        required: &[],
        switches: &[],
        positional: 0,
    },
    Spec { path: &["health", "wait"], flags: &["timeout"], required: &["timeout"], switches: &["deep"], positional: 0 },
    Spec { path: &["version", "switch"], flags: &[], required: &[], switches: &[], positional: 1 },
    Spec { path: &["version", "show"], flags: &[], required: &[], switches: &[], positional: 0 },
    Spec { path: &["update", "check"], flags: &[], required: &[], switches: &[], positional: 0 },
    Spec { path: &["update", "status"], flags: &[], required: &[], switches: &[], positional: 0 },
    Spec { path: &["update", "rollback"], flags: &["to", "reason"], required: &[], switches: &[], positional: 0 },
    Spec { path: &["update", "lock"], flags: &["owner", "ttl"], required: &["owner"], switches: &[], positional: 0 },
    Spec { path: &["update", "unlock"], flags: &["owner"], required: &["owner"], switches: &[], positional: 0 },
    Spec {
        path: &["tls", "setup"],
        flags: &["hostname"],
        required: &["hostname"],
        switches: &["import-root"],
        positional: 0,
    },
    Spec { path: &["kiosk", "rotate"], flags: &[], required: &[], switches: &[], positional: 0 },
    Spec { path: &["diag", "bundle"], flags: &["out"], required: &["out"], switches: &[], positional: 0 },
    Spec { path: &["migrate-from-v1"], flags: &[], required: &[], switches: &[], positional: 0 },
];

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Parsed {
    pub command: String,
    pub flags: BTreeMap<String, String>,
    pub switches: BTreeSet<String>,
    pub positional: Vec<String>,
    pub json: bool,
}

impl Parsed {
    pub fn flag(&self, name: &str) -> Option<&str> {
        self.flags.get(name).map(String::as_str)
    }
    /// Clave para `VMS_FAKE_VMSCTL_FAIL` («health wait» → «health-wait»).
    pub fn key(&self) -> String {
        self.command.replace(' ', "-")
    }
}

/// Analiza la línea de órdenes con la gramática de CONTRATO §14.1. `--json` y `--data-dir` valen en todas.
pub fn parse(args: &[String]) -> Result<Parsed, String> {
    let words: Vec<&str> = args.iter().map(String::as_str).take_while(|a| !a.starts_with("--")).collect();
    let spec = SPECS
        .iter()
        .filter(|s| s.path.len() <= words.len() && s.path.iter().zip(&words).all(|(a, b)| a == b))
        .max_by_key(|s| s.path.len())
        .ok_or_else(|| format!("orden desconocida: {}", args.join(" ")))?;
    let mut p = Parsed { command: spec.path.join(" "), ..Parsed::default() };
    let mut rest = args[spec.path.len()..].iter();
    while let Some(a) = rest.next() {
        if let Some(name) = a.strip_prefix("--") {
            if name == "json" {
                p.json = true;
            } else if spec.switches.contains(&name) {
                p.switches.insert(name.to_string());
            } else if spec.flags.contains(&name) || name == "data-dir" {
                let v = rest.next().ok_or_else(|| format!("falta el valor de --{name}"))?;
                p.flags.insert(name.to_string(), v.clone());
            } else {
                return Err(format!("opción desconocida para «{}»: --{name}", p.command));
            }
        } else {
            p.positional.push(a.clone());
        }
    }
    if p.positional.len() != spec.positional {
        return Err(format!(
            "«{}» espera {} argumento(s) y recibió {}",
            p.command,
            spec.positional,
            p.positional.len()
        ));
    }
    for r in spec.required {
        if !p.flags.contains_key(*r) {
            return Err(format!("«{}» necesita --{r}", p.command));
        }
    }
    validate(&p)?;
    Ok(p)
}

fn validate(p: &Parsed) -> Result<(), String> {
    match p.command.as_str() {
        "services install" => {
            let role = p.flag("role").unwrap_or_default();
            if !ROLES.contains(&role) {
                return Err(format!("rol desconocido: {role} (control, store, central o viewer)"));
            }
        }
        "firewall apply" => {
            let profiles = p.flag("profiles").unwrap_or_default();
            if profiles.split(',').any(|x| x != "private" && x != "domain") {
                return Err(format!("perfiles no admitidos: {profiles} (solo private y domain; nunca public)"));
            }
        }
        "health wait" => {
            p.flag("timeout")
                .unwrap_or_default()
                .parse::<u32>()
                .map_err(|_| "--timeout espera segundos".to_string())?;
        }
        "ports check" => {
            for f in ["http-port", "https-port"] {
                if let Some(v) = p.flag(f) {
                    let n: u32 = v.parse().map_err(|_| format!("--{f} no es un número"))?;
                    if n == 0 || n > 65535 {
                        return Err(format!("--{f} fuera de rango"));
                    }
                }
            }
        }
        "version switch" if !is_semver(&p.positional[0]) => {
            return Err(format!("versión no válida: {}", p.positional[0]));
        }
        _ => {}
    }
    Ok(())
}

pub fn is_semver(v: &str) -> bool {
    let core = v.split_once('-').map(|(c, _)| c).unwrap_or(v);
    let parts: Vec<&str> = core.split('.').collect();
    parts.len() == 3 && parts.iter().all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()))
}

/// `VMS_FAKE_VMSCTL_FAIL="health-wait=12,ports-check"` → código forzado para esta orden (por defecto 20).
pub fn forced_failure(env: Env, key: &str) -> Option<i32> {
    let spec = env("VMS_FAKE_VMSCTL_FAIL")?;
    spec.split(',').map(str::trim).find_map(|item| {
        let (k, code) = item.split_once('=').unwrap_or((item, "20"));
        (k == key).then(|| code.parse().unwrap_or(exit_codes::WINDOWS_ERROR))
    })
}

// ================================================================================================ efectos
/// Lo que el doble hace en el sistema. En Windows, `win::System`; en las pruebas, un registro en memoria.
pub trait Effects {
    fn elevated(&self) -> bool;
    fn user(&self) -> String;
    /// Para y elimina los servicios WinSW de la v1. Devuelve los que se quitaron.
    fn remove_v1_services(&mut self) -> Result<Vec<String>, String>;
}

pub struct Outcome {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

fn reply(json_out: bool, code: i32, data: Value, err: Option<(&str, String)>) -> Outcome {
    let error = err.as_ref().map(|(c, m)| json!({"code": c, "message_es": m, "win32": null})).unwrap_or(Value::Null);
    let stdout = if json_out {
        let mut obj = Map::new();
        obj.insert("ok".into(), json!(code == 0));
        obj.insert("code".into(), json!(code));
        obj.insert("data".into(), data);
        obj.insert("error".into(), error);
        format!("{}\n", Value::Object(obj))
    } else if code == 0 {
        "ok (doble de prueba)\n".to_string()
    } else {
        String::new()
    };
    let stderr = err.map(|(_, m)| format!("{m}\n")).unwrap_or_default();
    Outcome { code, stdout, stderr }
}

fn error_name(code: i32) -> &'static str {
    match code {
        exit_codes::PORT_IN_USE => "port_in_use",
        exit_codes::NO_PERMISSION => "no_permission",
        exit_codes::HEALTH_FAILED => "health_failed",
        exit_codes::USAGE => "usage",
        _ => "windows_error",
    }
}

/// Ejecuta una orden del doble. No escribe el registro de llamadas (eso lo hace `record_call`).
pub fn run_vmsctl(args: &[String], env: Env, fx: &mut dyn Effects) -> Outcome {
    if args.first().map(String::as_str) == Some("--version") {
        return Outcome { code: 0, stdout: format!("{DOUBLE_VERSION}\n"), stderr: String::new() };
    }
    let json_out = args.iter().any(|a| a == "--json");
    let p = match parse(args) {
        Ok(p) => p,
        Err(e) => return reply(json_out, exit_codes::USAGE, Value::Null, Some(("usage", e))),
    };
    if let Some(code) = forced_failure(env, &p.key()) {
        let msg = format!("Fallo simulado de «{}» (VMS_FAKE_VMSCTL_FAIL)", p.command);
        return reply(p.json, code, json!({"double": true}), Some((error_name(code), msg)));
    }
    let data_root = data_dir(env, p.flag("data-dir"));
    let mut data = json!({"double": true, "command": p.command});
    match p.command.as_str() {
        "run" => {
            let msg = "El doble de vmsctl no hospeda procesos: hace falta el vmsctl de B1".to_string();
            return reply(p.json, exit_codes::USAGE, data, Some(("usage", msg)));
        }
        "version switch" => match write_active(&data_root, &p.positional[0]) {
            Ok(v) => data["active"] = v,
            Err(e) => return reply(p.json, exit_codes::WINDOWS_ERROR, data, Some(("windows_error", e))),
        },
        "version show" => data["active"] = read_active(&data_root).unwrap_or(Value::Null),
        "services install" => {
            for sub in ["state", "logs"] {
                if let Err(e) = std::fs::create_dir_all(data_root.join(sub)) {
                    let msg = format!("No se pudo crear {}: {e}", data_root.join(sub).display());
                    return reply(p.json, exit_codes::WINDOWS_ERROR, data, Some(("windows_error", msg)));
                }
            }
        }
        "diag bundle" => {
            let out = PathBuf::from(p.flag("out").unwrap_or_default());
            let summary = format!(
                "Informe de diagnóstico del doble de vmsctl (sin secretos).\r\nCarpeta de datos: {}\r\n",
                data_root.display()
            );
            let entries = vec![("LEEME.txt".to_string(), summary.into_bytes())];
            if let Err(e) = write_zip(&out, &entries) {
                return reply(p.json, exit_codes::WINDOWS_ERROR, data, Some(("windows_error", e)));
            }
            data["out"] = json!(out.display().to_string());
        }
        "migrate-from-v1" => match fx.remove_v1_services() {
            Ok(removed) => data["removed_services"] = json!(removed),
            Err(e) => return reply(p.json, exit_codes::WINDOWS_ERROR, data, Some(("windows_error", e))),
        },
        "update lock" | "update unlock" => data["owner"] = json!(p.flag("owner")),
        "ports check" => data["ports"] = json!([8600, 8643, 8554, 8889, 8189, 9996, 9997, 9998]),
        _ => {}
    }
    reply(p.json, exit_codes::OK, data, None)
}

/// Añade una línea JSON al registro de llamadas. Si no se puede escribir, el doble falla: una prueba que no
/// ve sus llamadas no vale.
pub fn record_call(log: &Path, args: &[String], code: i32, fx: &dyn Effects) -> Result<(), String> {
    if let Some(parent) = log.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("No se pudo crear {}: {e}", parent.display()))?;
    }
    let cwd = std::env::current_dir().map(|p| p.display().to_string()).unwrap_or_default();
    let line = json!({
        "ts_unix": now_unix(), "argv": args, "exit": code, "elevated": fx.elevated(), "user": fx.user(),
        "cwd": cwd, "exe": std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default(),
    });
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)
        .map_err(|e| format!("No se pudo abrir {}: {e}", log.display()))?;
    writeln!(f, "{line}").map_err(|e| format!("No se pudo escribir {}: {e}", log.display()))
}

// ================================================================================================ active.json
pub fn active_path(data_root: &Path) -> PathBuf {
    data_root.join("state").join("active.json")
}

pub fn read_active(data_root: &Path) -> Option<Value> {
    let text = std::fs::read_to_string(active_path(data_root)).ok()?;
    serde_json::from_str::<Value>(&text).ok()?.get("active").cloned()
}

/// Escribe el puntero de CONTRATO §13.4 (temporal + renombrado, que en Windows es `MoveFileExW` con
/// `REPLACE_EXISTING`). Conserva los campos desconocidos y deja la versión anterior en `previous`.
pub fn write_active(data_root: &Path, version: &str) -> Result<Value, String> {
    let path = active_path(data_root);
    let dir = path.parent().ok_or("ruta de active.json sin carpeta")?;
    std::fs::create_dir_all(dir).map_err(|e| format!("No se pudo crear {}: {e}", dir.display()))?;
    let mut doc = std::fs::read_to_string(&path)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}));
    let previous = doc.get("active").cloned().filter(|v| v.as_str() != Some(version)).unwrap_or(Value::Null);
    let now = now_unix();
    doc["schema"] = json!(1);
    doc["active"] = json!(version);
    doc["previous"] = previous;
    doc["trial"] = json!(false);
    doc["trial_since_unix"] = Value::Null;
    if doc.get("updater").is_none_or(|u| !u.is_object()) {
        doc["updater"] = json!({"slot": "a", "previous_slot": null, "trial": false, "trial_since_unix": null});
    }
    doc["updated_unix"] = json!(now);
    let tmp = dir.join(format!("active.json.tmp-{}", std::process::id()));
    std::fs::write(&tmp, format!("{doc}\n")).map_err(|e| format!("No se pudo escribir {}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, &path).map_err(|e| format!("No se pudo sustituir {}: {e}", path.display()))?;
    Ok(json!(version))
}

// ================================================================================================ zip
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= u32::from(b);
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

/// Zip «stored» (sin compresión) válido: suficiente para el informe de diagnóstico del doble.
pub fn write_zip(path: &Path, entries: &[(String, Vec<u8>)]) -> Result<(), String> {
    let mut out: Vec<u8> = Vec::new();
    let mut central: Vec<u8> = Vec::new();
    for (name, data) in entries {
        let offset = u32::try_from(out.len()).map_err(|_| "zip demasiado grande")?;
        let crc = crc32(data);
        let size = u32::try_from(data.len()).map_err(|_| "entrada demasiado grande")?;
        let name_len = u16::try_from(name.len()).map_err(|_| "nombre demasiado largo")?;
        // Cabecera local
        out.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        out.extend_from_slice(&[20, 0, 0, 0, 0, 0, 0, 0, 0x21, 0]); // versión, flags, método 0, hora, fecha 1980-01-01
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&name_len.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(data);
        // Directorio central
        central.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        central.extend_from_slice(&[20, 0, 20, 0, 0, 0, 0, 0, 0, 0, 0x21, 0]);
        central.extend_from_slice(&crc.to_le_bytes());
        central.extend_from_slice(&size.to_le_bytes());
        central.extend_from_slice(&size.to_le_bytes());
        central.extend_from_slice(&name_len.to_le_bytes());
        central.extend_from_slice(&[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]); // extra, comentario, disco, atributos
        central.extend_from_slice(&offset.to_le_bytes());
        central.extend_from_slice(name.as_bytes());
    }
    let cd_offset = u32::try_from(out.len()).map_err(|_| "zip demasiado grande")?;
    let cd_size = u32::try_from(central.len()).map_err(|_| "zip demasiado grande")?;
    let count = u16::try_from(entries.len()).map_err(|_| "demasiadas entradas")?;
    out.extend_from_slice(&central);
    out.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    out.extend_from_slice(&[0, 0, 0, 0]);
    out.extend_from_slice(&count.to_le_bytes());
    out.extend_from_slice(&count.to_le_bytes());
    out.extend_from_slice(&cd_size.to_le_bytes());
    out.extend_from_slice(&cd_offset.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("No se pudo crear {}: {e}", parent.display()))?;
    }
    std::fs::write(path, out).map_err(|e| format!("No se pudo escribir {}: {e}", path.display()))
}

// ================================================================================================ vmshost
/// Ruta del visor de la versión activa: `<instalación>\versions\<activa>\viewer\VMS.exe` (CONTRATO §13.3).
pub fn viewer_path(install_root: &Path, data_root: &Path) -> Result<PathBuf, String> {
    let active = read_active(data_root)
        .and_then(|v| v.as_str().map(str::to_string))
        .ok_or_else(|| format!("No hay versión activa en {}", active_path(data_root).display()))?;
    let exe = install_root.join("versions").join(&active).join("viewer").join("VMS.exe");
    if exe.is_file() {
        Ok(exe)
    } else {
        Err(format!("No existe el visor de la versión activa: {}", exe.display()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(s: &str) -> Vec<String> {
        s.split_whitespace().map(String::from).collect()
    }

    struct Fx {
        removed: Vec<String>,
    }
    impl Effects for Fx {
        fn elevated(&self) -> bool {
            true
        }
        fn user(&self) -> String {
            "prueba".into()
        }
        fn remove_v1_services(&mut self) -> Result<Vec<String>, String> {
            Ok(self.removed.clone())
        }
    }

    fn no_env(_: &str) -> Option<String> {
        None
    }

    #[test]
    fn parses_contract_commands() {
        let p = parse(&args("services install --role store --data-dir C:\\datos --json")).unwrap();
        assert_eq!(p.command, "services install");
        assert_eq!(p.flag("role"), Some("store"));
        assert!(p.json);
        assert_eq!(parse(&args("health wait --timeout 120 --deep")).unwrap().switches.len(), 1);
        assert_eq!(parse(&args("version switch 2.0.0-ci.4")).unwrap().positional, vec!["2.0.0-ci.4"]);
        assert!(parse(&args("firewall apply --profiles private,domain")).is_ok());
        assert!(parse(&args("ports check --http-port 8601 --https-port 8644")).is_ok());
        assert!(parse(&args("update lock --owner installer --ttl 3600")).is_ok());
        assert!(parse(&args("migrate-from-v1 --json")).is_ok());
    }

    #[test]
    fn rejects_what_the_real_one_would_reject() {
        assert!(parse(&args("services install --role tienda --data-dir x")).is_err());
        assert!(parse(&args("services install --role store")).is_err());
        assert!(parse(&args("firewall apply --profiles public")).is_err());
        assert!(parse(&args("health wait")).is_err());
        assert!(parse(&args("version switch dos")).is_err());
        assert!(parse(&args("ports check --http-port 70000")).is_err());
        assert!(parse(&args("services install --role store --data-dir x --nope")).is_err());
        assert!(parse(&args("borrar todo")).is_err());
    }

    #[test]
    fn json_reply_follows_contract() {
        let mut fx = Fx { removed: vec![] };
        let out = run_vmsctl(&args("acl apply --data-dir C:\\d --json"), &no_env, &mut fx);
        assert_eq!(out.code, 0);
        let v: Value = serde_json::from_str(out.stdout.trim()).unwrap();
        assert_eq!(v["ok"], json!(true));
        assert_eq!(v["error"], Value::Null);
        let bad = run_vmsctl(&args("nada --json"), &no_env, &mut fx);
        assert_eq!(bad.code, exit_codes::USAGE);
    }

    #[test]
    fn forced_failures() {
        let env = |k: &str| (k == "VMS_FAKE_VMSCTL_FAIL").then(|| "health-wait=12, ports-check".to_string());
        let mut fx = Fx { removed: vec![] };
        let out = run_vmsctl(&args("health wait --timeout 5 --json"), &env, &mut fx);
        assert_eq!(out.code, 12);
        let v: Value = serde_json::from_str(out.stdout.trim()).unwrap();
        assert_eq!(v["error"]["code"], json!("health_failed"));
        assert_eq!(run_vmsctl(&args("ports check"), &env, &mut fx).code, 20);
        assert_eq!(run_vmsctl(&args("kiosk rotate"), &env, &mut fx).code, 0);
    }

    #[test]
    fn version_switch_writes_pointer_and_keeps_unknown_fields() {
        let dir = std::env::temp_dir().join(format!("vms-dobles-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("state")).unwrap();
        std::fs::write(active_path(&dir), r#"{"active":"1.9.0","x-extra":7}"#).unwrap();
        let d = dir.display().to_string();
        let env = move |k: &str| (k == "VMS_DATA_DIR").then(|| d.clone());
        let mut fx = Fx { removed: vec![] };
        assert_eq!(run_vmsctl(&args("version switch 2.0.0"), &env, &mut fx).code, 0);
        let v: Value = serde_json::from_str(&std::fs::read_to_string(active_path(&dir)).unwrap()).unwrap();
        assert_eq!(v["active"], json!("2.0.0"));
        assert_eq!(v["previous"], json!("1.9.0"));
        assert_eq!(v["x-extra"], json!(7));
        assert_eq!(v["schema"], json!(1));
        assert!(v["updater"].is_object());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn diag_bundle_is_a_valid_zip() {
        let dir = std::env::temp_dir().join(format!("vms-dobles-zip-{}", std::process::id()));
        let out = dir.join("diag.zip");
        write_zip(&out, &[("LEEME.txt".into(), b"hola".to_vec())]).unwrap();
        let bytes = std::fs::read(&out).unwrap();
        assert_eq!(&bytes[..4], &[0x50, 0x4b, 0x03, 0x04]);
        assert_eq!(&bytes[bytes.len() - 22..bytes.len() - 18], &[0x50, 0x4b, 0x05, 0x06]);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn migrate_reports_removed_services() {
        let mut fx = Fx { removed: vec!["VMSBackend".into()] };
        let out = run_vmsctl(&args("migrate-from-v1 --json"), &no_env, &mut fx);
        let v: Value = serde_json::from_str(out.stdout.trim()).unwrap();
        assert_eq!(v["data"]["removed_services"], json!(["VMSBackend"]));
    }

    #[test]
    fn semver() {
        assert!(is_semver("2.0.0") && is_semver("2.0.0-ci.12") && !is_semver("2.0") && !is_semver("a.b.c"));
    }
}
