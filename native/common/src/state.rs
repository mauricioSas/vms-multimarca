//! Puntero de versión `state\active.json`, diario `state\journal.json` y peticiones de vuelta atrás
//! (CONTRATO §13.3-§13.5).
//!
//! Reglas:
//! - El puntero solo lo escriben `VMSUpdater` (LocalSystem, o su `vmshost`) y el instalador (`vmsctl` elevado).
//!   Un `vmshost` de un servicio sin privilegios que detecta una versión a prueba rota deja una
//!   [`RollbackRequest`] en `state\requests\` y el `vmshost` del actualizador la ejecuta (hallazgo de S4).
//! - Los campos desconocidos del puntero y del diario se conservan al reescribirlos.
//! - Todo se escribe con [`crate::atomic_write`].

use crate::atomic_write;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::cmp::Ordering;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub const SCHEMA: u32 = 1;
/// Ranuras A/B del actualizador.
pub const SLOTS: [&str; 2] = ["a", "b"];

/// Segundos Unix (UTC). Sin dependencias de fecha (CONTRATO §13.4).
pub fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Error de estado con un mensaje claro en español.
#[derive(Debug)]
pub enum StateError {
    /// El archivo no existe.
    Missing(PathBuf),
    /// JSON no válido o con campos imposibles.
    Corrupt(PathBuf, String),
    /// Un valor recibido (versión, ranura) no es válido.
    Invalid(String),
    /// Error de E/S (incluye permiso denegado).
    Io(PathBuf, io::Error),
}

impl StateError {
    pub fn is_permission_denied(&self) -> bool {
        matches!(self, StateError::Io(_, e) if e.kind() == io::ErrorKind::PermissionDenied)
    }
}

impl fmt::Display for StateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StateError::Missing(p) => write!(f, "no existe {}", p.display()),
            StateError::Corrupt(p, why) => write!(f, "{} no es válido: {why}", p.display()),
            StateError::Invalid(why) => write!(f, "{why}"),
            StateError::Io(p, e) if e.kind() == io::ErrorKind::PermissionDenied => {
                write!(f, "sin permiso para escribir o leer {} (hace falta una consola de Administrador)", p.display())
            }
            StateError::Io(p, e) => write!(f, "error con {}: {e}", p.display()),
        }
    }
}

impl std::error::Error for StateError {}

/// Una versión es un nombre de carpeta dentro de `versions\`: sin separadores ni `..`.
pub fn validate_version(v: &str) -> Result<(), StateError> {
    let ok = !v.is_empty()
        && v.len() <= 64
        && !v.starts_with('.')
        && v.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | '+'));
    if ok {
        Ok(())
    } else {
        Err(StateError::Invalid(format!("versión no válida: «{v}» (solo letras, números, «.», «-», «_» y «+»)")))
    }
}

/// Precedencia SemVer 2.0 (`X.Y.Z[-pre][+build]`), la misma que `vms_updater.versioning.Version`.
/// `None` si alguna no es SemVer (quien la usa decide qué hacer en la duda).
pub fn compare_versions(a: &str, b: &str) -> Option<Ordering> {
    fn parse(v: &str) -> Option<([u64; 3], Vec<&str>)> {
        let core_pre = v.split('+').next()?;
        let (core, pre) = match core_pre.split_once('-') {
            Some((c, p)) => (c, p.split('.').collect::<Vec<_>>()),
            None => (core_pre, Vec::new()),
        };
        let nums: Vec<u64> = core.split('.').map(|n| n.parse().ok()).collect::<Option<_>>()?;
        if nums.len() != 3 || pre.iter().any(|p| p.is_empty()) {
            return None;
        }
        Some(([nums[0], nums[1], nums[2]], pre))
    }
    let (ca, pa) = parse(a)?;
    let (cb, pb) = parse(b)?;
    Some(ca.cmp(&cb).then_with(|| match (pa.is_empty(), pb.is_empty()) {
        (true, true) => Ordering::Equal,
        (true, false) => Ordering::Greater, // 2.0.0 > 2.0.0-rc.1
        (false, true) => Ordering::Less,
        (false, false) => {
            for (x, y) in pa.iter().zip(pb.iter()) {
                let o = match (x.parse::<u64>(), y.parse::<u64>()) {
                    (Ok(m), Ok(n)) => m.cmp(&n),
                    (Ok(_), Err(_)) => Ordering::Less,
                    (Err(_), Ok(_)) => Ordering::Greater,
                    (Err(_), Err(_)) => x.cmp(y),
                };
                if o != Ordering::Equal {
                    return o;
                }
            }
            pa.len().cmp(&pb.len())
        }
    }))
}

pub fn validate_slot(s: &str) -> Result<(), StateError> {
    if SLOTS.contains(&s) {
        Ok(())
    } else {
        Err(StateError::Invalid(format!("ranura del actualizador no válida: «{s}» (a o b)")))
    }
}

fn default_slot() -> String {
    "a".into()
}

/// Ranura del actualizador dentro del puntero.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct UpdaterSlot {
    #[serde(default = "default_slot")]
    pub slot: String,
    #[serde(default)]
    pub previous_slot: Option<String>,
    #[serde(default)]
    pub trial: bool,
    #[serde(default)]
    pub trial_since_unix: Option<u64>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Default for UpdaterSlot {
    fn default() -> Self {
        Self { slot: default_slot(), previous_slot: None, trial: false, trial_since_unix: None, extra: Map::new() }
    }
}

/// `state\active.json` (CONTRATO §13.4).
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Pointer {
    pub schema: u32,
    pub active: String,
    #[serde(default)]
    pub previous: Option<String>,
    #[serde(default)]
    pub trial: bool,
    #[serde(default)]
    pub trial_since_unix: Option<u64>,
    #[serde(default)]
    pub updater: UpdaterSlot,
    #[serde(default)]
    pub updated_unix: u64,
    /// Servicios que reinicia el último cambio de versión (CONTRATO §13.4). Lo escribe el actualizador en el
    /// paso `switched`. Un servicio que no está en la lista **no se relanza** por el cambio del puntero: sigue
    /// en su carpeta de versión (p. ej. el motor en una actualización solo de `app`, PLAN-V2 §2.5). Sin lista
    /// (`null` o ausente: instalador, punteros antiguos, puntero reconstruido) todos siguen al puntero.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub restart: Option<Vec<String>>,
    /// Campos de versiones futuras: se conservan.
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Pointer {
    pub fn new(active: &str) -> Self {
        Self {
            schema: SCHEMA,
            active: active.to_string(),
            previous: None,
            trial: false,
            trial_since_unix: None,
            updater: UpdaterSlot::default(),
            updated_unix: 0,
            restart: None,
            extra: Map::new(),
        }
    }

    /// ¿Tiene que pasar a la versión activa el servicio `service`, que ahora ejecuta `running`?
    ///
    /// Sí si el puntero no trae lista de reinicio, si el servicio está en ella o si `running` es **más nueva**
    /// que la activa (vuelta atrás: nunca se queda nadie en la versión que se abandonó). No en otro caso: un
    /// servicio sano no se corta por una actualización que no le toca. Si alguna versión no es SemVer, sí
    /// (como antes de existir la lista).
    pub fn follows(&self, service: &str, running: &str) -> bool {
        let Some(list) = &self.restart else { return true };
        list.iter().any(|s| s.eq_ignore_ascii_case(service))
            || compare_versions(running, &self.active).is_none_or(|o| o == Ordering::Greater)
    }

    fn check(&self) -> Result<(), String> {
        validate_version(&self.active).map_err(|e| e.to_string())?;
        if let Some(p) = &self.previous {
            validate_version(p).map_err(|e| e.to_string())?;
        }
        validate_slot(&self.updater.slot).map_err(|e| e.to_string())?;
        Ok(())
    }

    /// La versión activa pasa a `to`, «a prueba» desde `now`.
    ///
    /// `previous` guarda siempre una versión **confirmada**: si la activa sigue a prueba (o se vuelve a
    /// cambiar a la misma), se conserva el `previous` de antes. Así una vuelta atrás nunca cae en una
    /// versión que nadie confirmó. Volver a la confirmada mientras otra está a prueba es una vuelta atrás.
    pub fn switched(&self, to: &str, now: u64) -> Pointer {
        if self.trial && self.previous.as_deref() == Some(to) {
            return self.rolled_back(to);
        }
        let mut p = self.clone();
        if !self.trial && self.active != to {
            p.previous = Some(self.active.clone());
        }
        p.active = to.to_string();
        p.trial = true;
        p.trial_since_unix = Some(now);
        // Un cambio que no hace el actualizador (instalador, `vmsctl version switch`) no sabe qué servicios
        // reinicia: sin lista, todos siguen al puntero. El actualizador la pone con `with_restart`.
        p.restart = None;
        p
    }

    /// Fija la lista de servicios que reinicia este cambio de versión.
    pub fn with_restart(&self, services: &[&str]) -> Pointer {
        let mut p = self.clone();
        p.restart = Some(services.iter().map(|s| s.to_string()).collect());
        p
    }

    /// Confirmada: deja de estar a prueba.
    pub fn confirmed(&self) -> Pointer {
        let mut p = self.clone();
        p.trial = false;
        p.trial_since_unix = None;
        p
    }

    /// A qué versión volver desde la activa: primero la **última buena** del diario (`last_good`, la
    /// confirmada), después `previous`; nunca la activa ni una que no esté instalada.
    pub fn rollback_target(&self, last_good: Option<&str>, installed: &dyn Fn(&str) -> bool) -> Option<String> {
        [last_good.map(str::to_string), self.previous.clone()]
            .into_iter()
            .flatten()
            .find(|v| v != &self.active && installed(v))
    }

    /// Vuelta atrás a `to` (la anterior o la última buena), sin «a prueba».
    pub fn rolled_back(&self, to: &str) -> Pointer {
        let mut p = self.clone();
        p.previous = Some(self.active.clone());
        p.active = to.to_string();
        p.trial = false;
        p.trial_since_unix = None;
        p
    }

    /// Igual que [`Pointer::switched`]: `previous_slot` es siempre una ranura confirmada.
    pub fn slot_switched(&self, slot: &str, now: u64) -> Pointer {
        if self.updater.trial && self.updater.previous_slot.as_deref() == Some(slot) {
            return self.slot_rolled_back();
        }
        let mut p = self.clone();
        if !self.updater.trial && self.updater.slot != slot {
            p.updater.previous_slot = Some(self.updater.slot.clone());
        }
        p.updater.slot = slot.to_string();
        p.updater.trial = true;
        p.updater.trial_since_unix = Some(now);
        p
    }

    pub fn slot_confirmed(&self) -> Pointer {
        let mut p = self.clone();
        p.updater.trial = false;
        p.updater.trial_since_unix = None;
        p
    }

    /// Vuelta atrás de la ranura del actualizador. Sin ranura anterior, usa la otra.
    pub fn slot_rolled_back(&self) -> Pointer {
        let mut p = self.clone();
        let other = if self.updater.slot == "a" { "b" } else { "a" };
        let target = self.updater.previous_slot.clone().unwrap_or_else(|| other.to_string());
        p.updater.previous_slot = Some(self.updater.slot.clone());
        p.updater.slot = target;
        p.updater.trial = false;
        p.updater.trial_since_unix = None;
        p
    }
}

/// Clase de vuelta atrás que pide un `vmshost` sin privilegios.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum RollbackKind {
    /// La versión activa (`versions\<X>`).
    Version,
    /// La ranura del actualizador.
    Updater,
}

/// `state\requests\rollback-<Servicio>.json`: petición de vuelta atrás de una versión a prueba.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct RollbackRequest {
    pub schema: u32,
    pub service: String,
    pub kind: RollbackKind,
    /// Versión (o ranura) a prueba que falla. Solo se atiende si sigue siendo la activa y a prueba.
    pub from: String,
    pub reason: String,
    pub created_unix: u64,
}

/// Mensaje para el registro del arrancador: un evento (se anota siempre) o un problema (se anota solo
/// cuando cambia, para no llenar el registro cada medio segundo).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Note {
    Event(String),
    Problem(String),
}

/// Resultado de leer el puntero sin escribir nada.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub pointer: Pointer,
    /// `true` si el puntero faltaba, estaba dañado o apuntaba a una versión inexistente y se
    /// reconstruyó (en memoria) con `last_good` del diario.
    pub rebuilt: bool,
    /// Motivo de la reconstrucción (para el registro).
    pub why: String,
}

/// Carpeta `state\` de la carpeta de datos.
#[derive(Clone, Debug)]
pub struct StateDir {
    pub dir: PathBuf,
}

fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T, StateError> {
    let text = match std::fs::read(path) {
        Ok(t) => t,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Err(StateError::Missing(path.to_path_buf())),
        Err(e) => return Err(StateError::Io(path.to_path_buf(), e)),
    };
    serde_json::from_slice(&text).map_err(|e| StateError::Corrupt(path.to_path_buf(), e.to_string()))
}

fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), StateError> {
    let mut data = serde_json::to_vec_pretty(value).map_err(|e| StateError::Invalid(e.to_string()))?;
    data.push(b'\n');
    atomic_write(path, &data).map_err(|e| StateError::Io(path.to_path_buf(), e))
}

impl StateDir {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }
    pub fn pointer_path(&self) -> PathBuf {
        self.dir.join("active.json")
    }
    pub fn journal_path(&self) -> PathBuf {
        self.dir.join("journal.json")
    }
    pub fn requests_dir(&self) -> PathBuf {
        self.dir.join("requests")
    }
    pub fn request_path(&self, service: &str) -> PathBuf {
        self.requests_dir().join(format!("rollback-{service}.json"))
    }
    /// Última vuelta atrás hecha por un `vmshost` (diagnóstico para el actualizador y `diag bundle`).
    pub fn host_rollback_path(&self) -> PathBuf {
        self.dir.join("host-rollback.json")
    }

    pub fn read_pointer(&self) -> Result<Pointer, StateError> {
        let path = self.pointer_path();
        let p: Pointer = read_json(&path)?;
        p.check().map_err(|why| StateError::Corrupt(path, why))?;
        Ok(p)
    }

    /// Escribe el puntero (pone `updated_unix`). Solo actualizador e instalador.
    pub fn write_pointer(&self, p: &Pointer) -> Result<Pointer, StateError> {
        p.check().map_err(StateError::Invalid)?;
        let mut p = p.clone();
        p.schema = SCHEMA;
        p.updated_unix = now_unix();
        write_json(&self.pointer_path(), &p)?;
        Ok(p)
    }

    /// Diario completo como objeto JSON (sus campos son del actualizador, B4).
    pub fn read_journal(&self) -> Result<Map<String, Value>, StateError> {
        read_json(&self.journal_path())
    }

    pub fn last_good(&self) -> Result<String, StateError> {
        let path = self.journal_path();
        let j = self.read_journal()?;
        match j.get("last_good").and_then(Value::as_str) {
            Some(v) if validate_version(v).is_ok() => Ok(v.to_string()),
            _ => Err(StateError::Corrupt(path, "falta «last_good» o no es una versión válida".into())),
        }
    }

    /// Fija `last_good` conservando el resto del diario (o lo crea en estado `idle`).
    pub fn set_last_good(&self, version: &str) -> Result<(), StateError> {
        validate_version(version)?;
        let mut j = match self.read_journal() {
            Ok(j) => j,
            Err(StateError::Missing(_)) | Err(StateError::Corrupt(..)) => {
                let mut m = Map::new();
                m.insert("schema".into(), Value::from(SCHEMA));
                m.insert("state".into(), Value::from("idle"));
                m
            }
            Err(e) => return Err(e),
        };
        j.insert("last_good".into(), Value::from(version));
        write_json(&self.journal_path(), &Value::Object(j))
    }

    /// Lee el puntero y, si falta, está dañado o apunta a una versión que no existe
    /// (`exists(version) == false`), lo reconstruye **en memoria** con `last_good` del diario.
    /// No escribe nada: quien tenga permiso decide si lo guarda.
    pub fn resolve(&self, exists: impl Fn(&str) -> bool) -> Result<Resolved, StateError> {
        let why = match self.read_pointer() {
            Ok(p) if exists(&p.active) => return Ok(Resolved { pointer: p, rebuilt: false, why: String::new() }),
            Ok(p) => format!("el puntero apunta a {}, que no está instalada", p.active),
            Err(e) => e.to_string(),
        };
        let good = self.last_good().map_err(|e| {
            StateError::Invalid(format!("no hay versión activa ({why}) ni versión buena en el diario ({e})"))
        })?;
        if !exists(&good) {
            return Err(StateError::Invalid(format!(
                "no hay versión activa ({why}) y la última buena ({good}) no está instalada"
            )));
        }
        // Se conservan los campos conocidos que sigan valiendo (ranura del actualizador y extras).
        let mut pointer = Pointer::new(&good);
        if let Ok(old) = self.read_pointer() {
            pointer.updater = old.updater;
            pointer.extra = old.extra;
        }
        Ok(Resolved { pointer, rebuilt: true, why })
    }

    /// Vuelta atrás de la versión activa `p` (la usa el arrancador del actualizador): escribe el puntero y
    /// `host-rollback.json` (diagnóstico). Devuelve el puntero nuevo.
    pub fn roll_back_version(
        &self,
        p: &Pointer,
        why: &str,
        unix: u64,
        installed: &dyn Fn(&str) -> bool,
    ) -> Result<Pointer, StateError> {
        let last_good = self.last_good().ok();
        let to = p.rollback_target(last_good.as_deref(), installed).ok_or_else(|| {
            StateError::Invalid(format!(
                "la versión a prueba {} falla ({why}) pero no hay otra instalada a la que volver",
                p.active
            ))
        })?;
        let np = self.write_pointer(&p.rolled_back(&to))?;
        let rec = serde_json::json!({
            "schema": SCHEMA, "from": p.active, "to": to, "reason": why, "at_unix": unix, "by": "vmshost",
        });
        // Solo diagnóstico: si no se puede escribir, la vuelta atrás ya está hecha.
        let _ = write_json(&self.host_rollback_path(), &rec);
        Ok(np)
    }

    /// Tareas del arrancador de `VMSUpdater` (el único, con el instalador, que escribe el puntero): atiende
    /// las peticiones de vuelta atrás de `state\requests\` (solo si piden la versión que sigue a prueba) y
    /// vuelve atrás la versión a prueba que nadie confirmó en `confirm_timeout_s`.
    pub fn updater_duties(&self, unix: u64, confirm_timeout_s: u64, installed: &dyn Fn(&str) -> bool) -> Vec<Note> {
        let mut notes = Vec::new();
        let mut pointer = self.read_pointer();
        for (path, req) in self.read_requests() {
            match (&req, &pointer) {
                (Err(e), _) => notes.push(Note::Event(format!("petición ilegible descartada: {e}"))),
                (Ok(r), Ok(p)) if r.kind == RollbackKind::Version && p.trial && p.active == r.from => {
                    let why = format!("{} lo pidió: {}", r.service, r.reason);
                    notes.push(match self.roll_back_version(p, &why, unix, installed) {
                        Ok(np) => Note::Event(format!("vuelta atrás de {} a {}: {why}", p.active, np.active)),
                        Err(e) => Note::Problem(format!("no se pudo volver atrás: {e}")),
                    });
                    pointer = self.read_pointer();
                }
                (Ok(r), _) => notes.push(Note::Event(format!(
                    "petición de {} sobre {} descartada: ya no es la versión a prueba",
                    r.service, r.from
                ))),
            }
            if let Err(e) = std::fs::remove_file(&path) {
                if e.kind() != io::ErrorKind::NotFound {
                    notes.push(Note::Problem(format!("no se pudo borrar {}: {e}", path.display())));
                }
            }
        }
        if let Ok(p) = self.read_pointer() {
            if trial_expired(p.trial, p.trial_since_unix, unix, confirm_timeout_s) {
                let why = format!("nadie confirmó {} en {} min", p.active, confirm_timeout_s / 60);
                notes.push(match self.roll_back_version(&p, &why, unix, installed) {
                    Ok(np) => Note::Event(format!("vuelta atrás de {} a {}: {why}", p.active, np.active)),
                    Err(e) => Note::Problem(format!("no se pudo volver atrás: {e}")),
                });
            }
        }
        notes
    }

    pub fn write_request(&self, req: &RollbackRequest) -> Result<PathBuf, StateError> {
        let path = self.request_path(&req.service);
        write_json(&path, req)?;
        Ok(path)
    }

    /// Peticiones pendientes (las ilegibles se devuelven como error para registrarlas y borrarlas).
    pub fn read_requests(&self) -> Vec<(PathBuf, Result<RollbackRequest, StateError>)> {
        let Ok(entries) = std::fs::read_dir(self.requests_dir()) else {
            return Vec::new();
        };
        let mut out: Vec<(PathBuf, Result<RollbackRequest, StateError>)> = entries
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| {
                let n = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                n.starts_with("rollback-") && n.ends_with(".json")
            })
            .map(|p| {
                let r = read_json::<RollbackRequest>(&p);
                (p, r)
            })
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }
}

/// ¿Ha pasado el plazo para confirmar la versión (o ranura) a prueba?
pub fn trial_expired(trial: bool, since: Option<u64>, now: u64, timeout_s: u64) -> bool {
    trial && since.is_some_and(|t| now.saturating_sub(t) >= timeout_s)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir_with(versions: &[&str]) -> (tempfile::TempDir, StateDir, impl Fn(&str) -> bool) {
        let d = tempfile::tempdir().unwrap();
        let s = StateDir::new(d.path().join("state"));
        let known: Vec<String> = versions.iter().map(|v| v.to_string()).collect();
        (d, s, move |v: &str| known.iter().any(|k| k == v))
    }

    #[test]
    fn versions_are_validated_against_path_traversal() {
        for bad in ["", "..", ".", "../x", "a\\b", "a/b", "C:", "x y", &"9".repeat(65)] {
            assert!(validate_version(bad).is_err(), "{bad} debería rechazarse");
        }
        for good in ["2.0.0", "2.1.0-dev.5", "2.0.0+ci", "2.0.0_rc1"] {
            assert!(validate_version(good).is_ok(), "{good}");
        }
        assert!(validate_slot("a").is_ok() && validate_slot("c").is_err());
    }

    #[test]
    fn missing_pointer_is_rebuilt_in_memory_from_journal() {
        let (_d, s, exists) = dir_with(&["1.0.0"]);
        assert!(matches!(s.resolve(&exists), Err(StateError::Invalid(_))), "sin diario no hay a qué volver");
        s.set_last_good("1.0.0").unwrap();
        let r = s.resolve(&exists).unwrap();
        assert!(r.rebuilt && r.pointer.active == "1.0.0" && !r.pointer.trial);
        assert!(!s.pointer_path().exists(), "resolve no escribe");
    }

    #[test]
    fn corrupt_pointer_or_missing_version_is_rebuilt() {
        let (_d, s, exists) = dir_with(&["1.0.0"]);
        s.set_last_good("1.0.0").unwrap();
        std::fs::create_dir_all(&s.dir).unwrap();
        std::fs::write(s.pointer_path(), b"{basura").unwrap();
        let r = s.resolve(&exists).unwrap();
        assert!(r.rebuilt && r.why.contains("no es válido"), "{}", r.why);
        s.write_pointer(&Pointer::new("9.9.9")).unwrap();
        let r = s.resolve(&exists).unwrap();
        assert!(r.rebuilt && r.pointer.active == "1.0.0" && r.why.contains("9.9.9"));
        // Un puntero con una versión imposible (ruta) también se trata como dañado
        std::fs::write(s.pointer_path(), br#"{"schema":1,"active":"..\\..\\Windows"}"#).unwrap();
        assert!(s.resolve(&exists).unwrap().rebuilt);
    }

    #[test]
    fn last_good_not_installed_is_an_error() {
        let (_d, s, exists) = dir_with(&["2.0.0"]);
        s.set_last_good("1.0.0").unwrap();
        let e = s.resolve(&exists).unwrap_err().to_string();
        assert!(e.contains("1.0.0") && e.contains("no está instalada"), "{e}");
    }

    #[test]
    fn switch_confirm_and_rollback_keep_unknown_fields() {
        let (_d, s, _) = dir_with(&["1.0.0", "1.1.0"]);
        std::fs::create_dir_all(&s.dir).unwrap();
        let contract = r#"{"schema": 1, "active": "1.0.0", "previous": null, "trial": false,
            "trial_since_unix": null, "updated_unix": 1763600000, "x-futuro": {"a": 1},
            "updater": {"slot": "b", "previous_slot": "a", "trial": false, "trial_since_unix": null, "x-u": 2}}"#;
        std::fs::write(s.pointer_path(), contract).unwrap();
        let p = s.read_pointer().unwrap();
        let p = s.write_pointer(&p.switched("1.1.0", 1000)).unwrap();
        assert!(p.trial && p.previous.as_deref() == Some("1.0.0") && p.trial_since_unix == Some(1000));
        let p = s.write_pointer(&p.rolled_back("1.0.0")).unwrap();
        assert_eq!((p.active.as_str(), p.trial, p.previous.as_deref()), ("1.0.0", false, Some("1.1.0")));
        let v: Value = serde_json::from_slice(&std::fs::read(s.pointer_path()).unwrap()).unwrap();
        assert_eq!(v["x-futuro"]["a"], 1);
        assert_eq!(v["updater"]["slot"], "b");
        assert_eq!(v["updater"]["x-u"], 2);
        assert!(v["updated_unix"].as_u64().unwrap() > 1763600000);
        assert!(v.get("trial_since_unix").is_some() && v.get("trial_since").is_none());
    }

    #[test]
    fn switching_again_while_on_trial_keeps_the_last_confirmed_as_previous() {
        // Regresión: 2.0.0 (buena) → 2.1.0 (a prueba) → 2.1.1 (a prueba) → la anterior sigue siendo 2.0.0
        let p = Pointer::new("2.0.0").switched("2.1.0", 10).switched("2.1.1", 20);
        assert_eq!((p.active.as_str(), p.previous.as_deref(), p.trial), ("2.1.1", Some("2.0.0"), true));
        // Cambiar a la misma versión a prueba no deja previous == active
        let same = Pointer::new("2.0.0").switched("2.1.0", 10).switched("2.1.0", 30);
        assert_eq!(same.previous.as_deref(), Some("2.0.0"));
        assert_eq!(same.trial_since_unix, Some(30));
        // Tras confirmar, el siguiente cambio sí guarda la confirmada
        let c = Pointer::new("2.0.0").switched("2.1.0", 10).confirmed().switched("2.2.0", 40);
        assert_eq!(c.previous.as_deref(), Some("2.1.0"));
        // Volver a la confirmada mientras otra está a prueba es una vuelta atrás (no queda a prueba)
        let back = Pointer::new("2.0.0").switched("2.1.0", 10).switched("2.0.0", 20);
        assert_eq!((back.active.as_str(), back.trial, back.previous.as_deref()), ("2.0.0", false, Some("2.1.0")));
        // Lo mismo para la ranura del actualizador
        let s = Pointer::new("2.0.0").slot_switched("b", 1).slot_switched("a", 2);
        assert_eq!((s.updater.slot.as_str(), s.updater.trial), ("a", false));
        let s = Pointer::new("2.0.0").slot_switched("b", 1).slot_switched("b", 2);
        assert_eq!(s.updater.previous_slot.as_deref(), Some("a"));
    }

    #[test]
    fn rollback_prefers_last_good_over_an_unconfirmed_previous() {
        let installed = |v: &str| ["2.0.0", "2.1.0", "2.1.1"].contains(&v);
        let mut p = Pointer::new("2.1.1");
        p.previous = Some("2.1.0".into()); // puntero antiguo (de antes de esta corrección) o editado a mano
        p.trial = true;
        assert_eq!(p.rollback_target(Some("2.0.0"), &installed).as_deref(), Some("2.0.0"));
        // Sin diario, la anterior
        assert_eq!(p.rollback_target(None, &installed).as_deref(), Some("2.1.0"));
        // last_good == activa (vuelta atrás manual de una confirmada): la anterior
        let mut q = Pointer::new("2.1.0");
        q.previous = Some("2.0.0".into());
        assert_eq!(q.rollback_target(Some("2.1.0"), &installed).as_deref(), Some("2.0.0"));
        // Nunca una que no esté instalada
        assert_eq!(p.rollback_target(Some("1.0.0"), &|v| v == "2.1.1"), None);
    }

    #[test]
    fn slot_switch_and_rollback() {
        let p = Pointer::new("2.0.0");
        let p = p.slot_switched("b", 50);
        assert_eq!((p.updater.slot.as_str(), p.updater.trial), ("b", true));
        let back = p.slot_rolled_back();
        assert_eq!((back.updater.slot.as_str(), back.updater.trial), ("a", false));
        let mut lone = Pointer::new("2.0.0");
        lone.updater.slot = "b".into();
        assert_eq!(lone.slot_rolled_back().updater.slot, "a", "sin ranura anterior vuelve a la otra");
        assert!(!p.slot_confirmed().updater.trial);
    }

    #[test]
    fn journal_last_good_is_merged_not_replaced() {
        let (_d, s, _) = dir_with(&[]);
        std::fs::create_dir_all(&s.dir).unwrap();
        std::fs::write(s.journal_path(), br#"{"schema":1,"state":"verifying","update_id":"u-1","last_good":"1.0.0"}"#)
            .unwrap();
        s.set_last_good("1.1.0").unwrap();
        let j = s.read_journal().unwrap();
        assert_eq!(j["last_good"], "1.1.0");
        assert_eq!(j["update_id"], "u-1");
        assert_eq!(j["state"], "verifying");
        assert!(s.set_last_good("../x").is_err());
    }

    #[test]
    fn requests_roundtrip_and_ignore_other_files() {
        let (_d, s, _) = dir_with(&[]);
        let req = RollbackRequest {
            schema: 1,
            service: "VMSBackend".into(),
            kind: RollbackKind::Version,
            from: "2.1.0".into(),
            reason: "3 caídas en 10 min".into(),
            created_unix: 5,
        };
        s.write_request(&req).unwrap();
        std::fs::write(s.requests_dir().join("otra-cosa.txt"), b"x").unwrap();
        std::fs::write(s.requests_dir().join("rollback-Roto.json"), b"{").unwrap();
        let all = s.read_requests();
        assert_eq!(all.len(), 2);
        assert!(all[0].0.ends_with("rollback-Roto.json") && all[0].1.is_err());
        assert_eq!(all[1].1.as_ref().unwrap(), &req);
        let v: Value = serde_json::from_slice(&std::fs::read(s.request_path("VMSBackend")).unwrap()).unwrap();
        assert_eq!(v["kind"], "version");
    }

    #[test]
    fn semver_precedence_matches_the_updater() {
        use Ordering::*;
        for (a, b, o) in [
            ("2.1.0", "2.0.0", Greater),
            ("2.0.10", "2.0.9", Greater),
            ("2.0.0", "2.0.0-rc.1", Greater),
            ("2.0.0-rc.2", "2.0.0-rc.10", Less),
            ("2.0.0-dev.5", "2.0.0-rc.1", Less),
            ("2.0.0-1", "2.0.0-alpha", Less),
            ("2.0.0-alpha", "2.0.0-alpha.1", Less),
            ("2.0.0+ci", "2.0.0", Equal),
        ] {
            assert_eq!(compare_versions(a, b), Some(o), "{a} vs {b}");
        }
        assert_eq!(compare_versions("2.0", "2.0.0"), None);
        assert_eq!(compare_versions("x", "2.0.0"), None);
    }

    #[test]
    fn restart_list_decides_who_follows_the_pointer() {
        let base = Pointer::new("2.0.0").switched("2.1.0", 10);
        assert!(base.restart.is_none() && base.follows("VMSEngine", "2.0.0"), "sin lista: todos");
        let p = base.with_restart(&["VMSBackend", "VMSAnalytics"]);
        assert!(p.follows("VMSBackend", "2.0.0") && p.follows("vmsanalytics", "2.0.0"));
        assert!(!p.follows("VMSEngine", "2.0.0"), "el motor no se reinicia por una de app");
        assert!(!p.confirmed().follows("VMSEngine", "2.0.0"), "ni al confirmarla");
        // Vuelta atrás: la lista se conserva, y quien corre la versión abandonada vuelve
        let back = p.rolled_back("2.0.0");
        assert_eq!(back.restart, p.restart);
        assert!(back.follows("VMSEngine", "2.1.0") && !back.follows("VMSEngine", "1.9.0"));
        // El siguiente cambio sin lista (instalador) la borra
        assert!(p.confirmed().switched("2.2.0", 20).restart.is_none());
        // Versiones raras: se sigue (como antes)
        assert!(p.follows("VMSEngine", "rara"));
    }

    #[test]
    fn restart_list_roundtrips_and_is_omitted_when_absent() {
        let (_d, s, _) = dir_with(&[]);
        let p = s.write_pointer(&Pointer::new("2.0.0").switched("2.1.0", 1).with_restart(&["VMSBackend"])).unwrap();
        let v: Value = serde_json::from_slice(&std::fs::read(s.pointer_path()).unwrap()).unwrap();
        assert_eq!(v["restart"], serde_json::json!(["VMSBackend"]));
        assert_eq!(s.read_pointer().unwrap().restart, p.restart);
        s.write_pointer(&Pointer::new("2.0.0")).unwrap();
        let v: Value = serde_json::from_slice(&std::fs::read(s.pointer_path()).unwrap()).unwrap();
        assert!(v.get("restart").is_none());
        // `null` (lo escribe el actualizador en Python sin lista) = sin lista
        std::fs::write(s.pointer_path(), br#"{"schema":1,"active":"2.0.0","restart":null}"#).unwrap();
        assert!(s.read_pointer().unwrap().restart.is_none());
    }

    #[test]
    fn trial_expiry() {
        assert!(!trial_expired(true, Some(1000), 1010, 30));
        assert!(trial_expired(true, Some(1000), 1030, 30));
        assert!(!trial_expired(false, Some(1000), 99999, 30));
        assert!(!trial_expired(true, None, 99999, 30));
    }
}
