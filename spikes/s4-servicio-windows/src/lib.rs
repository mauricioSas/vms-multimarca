//! Lógica portable de la prueba S4: puntero `active.json`, diario mínimo y vigilancia de la versión a prueba.
//!
//! Formato del puntero (CONTRATO §13.4, mismos nombres de campo):
//! `{"schema":1,"active":"1.1.0","previous":"1.0.0","trial":true,"trial_since_unix":1759622400,"updated_unix":1759622400}`
//! Los campos que esta prueba no usa (p. ej. el objeto `updater`) se conservan tal cual al reescribirlo.
//! El diario (`journal.json`) solo guarda aquí `last_good`, que es lo que necesita el arrancador
//! para reconstruir un puntero ausente o corrupto.

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use vms_common::atomic_write;

pub fn now_s() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq, Eq)]
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
    pub updated_unix: u64,
    /// Campos desconocidos (`updater`, los de versiones futuras): se conservan (CONTRATO §13.4).
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Journal {
    pub last_good: String,
}

pub struct Layout {
    pub root: PathBuf,
}

impl Layout {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }
    pub fn pointer(&self) -> PathBuf {
        self.root.join("state").join("active.json")
    }
    pub fn journal(&self) -> PathBuf {
        self.root.join("state").join("journal.json")
    }
    pub fn status(&self) -> PathBuf {
        self.root.join("state").join("host-status.json")
    }
    pub fn log(&self) -> PathBuf {
        self.root.join("logs").join("vmshost.log")
    }
    pub fn version_dir(&self, v: &str) -> PathBuf {
        self.root.join("versions").join(v)
    }

    pub fn read_pointer(&self) -> io::Result<Pointer> {
        let text = std::fs::read_to_string(self.pointer())?;
        serde_json::from_str(&text).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }

    pub fn write_pointer(&self, p: &Pointer) -> io::Result<()> {
        let mut p = p.clone();
        p.updated_unix = now_s();
        atomic_write(&self.pointer(), &serde_json::to_vec_pretty(&p).map_err(io::Error::other)?)
    }

    pub fn read_journal(&self) -> io::Result<Journal> {
        let text = std::fs::read_to_string(self.journal())?;
        serde_json::from_str(&text).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }

    pub fn write_journal(&self, j: &Journal) -> io::Result<()> {
        atomic_write(&self.journal(), &serde_json::to_vec_pretty(j).map_err(io::Error::other)?)
    }

    /// Lee el puntero; si falta o está corrupto lo reconstruye con la última versión buena del diario.
    /// Devuelve `(puntero, reconstruido)`.
    pub fn load_or_rebuild(&self) -> io::Result<(Pointer, bool)> {
        match self.read_pointer() {
            Ok(p) if !p.active.is_empty() && self.version_dir(&p.active).is_dir() => Ok((p, false)),
            _ => {
                let j = self.read_journal()?;
                let p = Pointer { schema: 1, active: j.last_good, ..Pointer::default() };
                self.write_pointer(&p)?;
                Ok((p, true))
            }
        }
    }

    /// `vmsctl version switch`: la versión nueva pasa a estar activa «a prueba».
    pub fn switch(&self, to: &str) -> io::Result<Pointer> {
        let (cur, _) = self.load_or_rebuild()?;
        let p = Pointer {
            schema: 1,
            active: to.to_string(),
            previous: Some(cur.active),
            trial: true,
            trial_since_unix: Some(now_s()),
            updated_unix: 0,
            extra: cur.extra,
        };
        self.write_pointer(&p)?;
        Ok(p)
    }

    /// El actualizador confirma la versión a prueba: deja de estar «a prueba» y pasa a ser la última buena.
    pub fn confirm(&self) -> io::Result<Pointer> {
        let (mut p, _) = self.load_or_rebuild()?;
        p.trial = false;
        p.trial_since_unix = None;
        self.write_pointer(&p)?;
        self.write_journal(&Journal { last_good: p.active.clone() })?;
        Ok(p)
    }

    /// Vuelta atrás: la anterior pasa a ser la activa (no a prueba).
    pub fn rollback(&self) -> io::Result<Pointer> {
        let (p, _) = self.load_or_rebuild()?;
        let target = match p.previous.clone() {
            Some(v) => v,
            None => self.read_journal()?.last_good,
        };
        let np = Pointer {
            schema: 1,
            active: target,
            previous: Some(p.active),
            trial: false,
            trial_since_unix: None,
            updated_unix: 0,
            extra: p.extra,
        };
        self.write_pointer(&np)?;
        Ok(np)
    }
}

/// ¿Ha pasado el plazo para confirmar la versión a prueba?
pub fn trial_expired(p: &Pointer, now: u64, timeout: Duration) -> bool {
    p.trial && p.trial_since_unix.is_some_and(|t| now.saturating_sub(t) >= timeout.as_secs())
}

/// Espera creciente entre relanzamientos de un hijo que cae: 1, 2, 5, 10 y 30 s (como `vmsctl run`,
/// CONTRATO §14.1). Vuelve a empezar si el hijo aguantó `stable` en marcha.
pub struct Backoff {
    step: usize,
    stable: Duration,
}

pub const BACKOFF_S: [u64; 5] = [1, 2, 5, 10, 30];

impl Backoff {
    pub fn new(stable: Duration) -> Self {
        Self { step: 0, stable }
    }
    /// Espera antes de relanzar un hijo que cayó tras `ran` en marcha.
    pub fn after_crash(&mut self, ran: Duration) -> Duration {
        if ran >= self.stable {
            self.step = 0;
        }
        let wait = BACKOFF_S[self.step.min(BACKOFF_S.len() - 1)];
        self.step += 1;
        Duration::from_secs(wait)
    }
    pub fn reset(&mut self) {
        self.step = 0;
    }
}

/// Ventana de caídas: «3 caídas en 10 minutos».
pub struct CrashWindow {
    limit: usize,
    window: Duration,
    times: VecDeque<Instant>,
}

impl CrashWindow {
    pub fn new(limit: usize, window: Duration) -> Self {
        Self { limit, window, times: VecDeque::new() }
    }
    /// Anota una caída y devuelve `true` si ya se alcanzó el límite dentro de la ventana.
    pub fn record(&mut self, at: Instant) -> bool {
        self.times.push_back(at);
        while let Some(&first) = self.times.front() {
            if at.duration_since(first) > self.window {
                self.times.pop_front();
            } else {
                break;
            }
        }
        self.times.len() >= self.limit
    }
    pub fn count(&self) -> usize {
        self.times.len()
    }
    pub fn reset(&mut self) {
        self.times.clear();
    }
}

pub fn append_log(path: &Path, line: &str) {
    use std::io::Write;
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{} {}", now_s(), vms_common::redact(line));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn layout_with(versions: &[&str]) -> (tempfile::TempDir, Layout) {
        let dir = tempfile::tempdir().unwrap();
        for v in versions {
            std::fs::create_dir_all(dir.path().join("versions").join(v)).unwrap();
        }
        let l = Layout::new(dir.path());
        l.write_journal(&Journal { last_good: versions[0].to_string() }).unwrap();
        (dir, l)
    }

    #[test]
    fn missing_or_corrupt_pointer_is_rebuilt_from_journal() {
        let (_d, l) = layout_with(&["1.0.0", "2.0.0"]);
        let (p, rebuilt) = l.load_or_rebuild().unwrap();
        assert!(rebuilt);
        assert_eq!(p.active, "1.0.0");
        std::fs::write(l.pointer(), b"{basura").unwrap();
        let (p, rebuilt) = l.load_or_rebuild().unwrap();
        assert!(rebuilt && p.active == "1.0.0" && !p.trial);
    }

    #[test]
    fn pointer_to_a_missing_version_is_rebuilt() {
        let (_d, l) = layout_with(&["1.0.0"]);
        l.write_pointer(&Pointer { schema: 1, active: "9.9.9".into(), ..Pointer::default() }).unwrap();
        assert_eq!(l.load_or_rebuild().unwrap().0.active, "1.0.0");
    }

    #[test]
    fn switch_confirm_and_rollback() {
        let (_d, l) = layout_with(&["1.0.0", "1.1.0"]);
        let p = l.switch("1.1.0").unwrap();
        assert!(p.trial && p.previous.as_deref() == Some("1.0.0"));
        let p = l.rollback().unwrap();
        assert_eq!((p.active.as_str(), p.trial), ("1.0.0", false));
        l.switch("1.1.0").unwrap();
        let p = l.confirm().unwrap();
        assert!(!p.trial);
        assert_eq!(l.read_journal().unwrap().last_good, "1.1.0");
    }

    #[test]
    fn crash_window_counts_only_recent_crashes() {
        let mut w = CrashWindow::new(3, Duration::from_secs(600));
        let t0 = Instant::now();
        assert!(!w.record(t0));
        assert!(!w.record(t0 + Duration::from_secs(700))); // la primera ya salió de la ventana
        assert!(!w.record(t0 + Duration::from_secs(800)));
        assert!(w.record(t0 + Duration::from_secs(900)));
    }

    #[test]
    fn trial_expires_after_timeout() {
        let p = Pointer {
            schema: 1,
            active: "1.1.0".into(),
            previous: Some("1.0.0".into()),
            trial: true,
            trial_since_unix: Some(1000),
            ..Pointer::default()
        };
        assert!(!trial_expired(&p, 1010, Duration::from_secs(30)));
        assert!(trial_expired(&p, 1030, Duration::from_secs(30)));
    }

    #[test]
    fn pointer_uses_contract_names_and_keeps_unknown_fields() {
        let (_d, l) = layout_with(&["1.0.0", "1.1.0"]);
        let contract = r#"{"schema": 1, "active": "1.0.0", "previous": null, "trial": false,
            "trial_since_unix": null, "updated_unix": 1763600000,
            "updater": {"slot": "b", "previous_slot": "a", "trial": false, "trial_since_unix": null}}"#;
        std::fs::write(l.pointer(), contract).unwrap();
        l.switch("1.1.0").unwrap();
        l.rollback().unwrap();
        let v: serde_json::Value = serde_json::from_slice(&std::fs::read(l.pointer()).unwrap()).unwrap();
        assert_eq!(v["updater"]["slot"], "b");
        assert_eq!(v["active"], "1.0.0");
        assert!(v["updated_unix"].as_u64().unwrap() > 1763600000);
        assert!(v.get("trial_since_unix").is_some() && v.get("trial_since").is_none() && v.get("updated").is_none());
    }

    #[test]
    fn backoff_grows_and_restarts_after_a_stable_run() {
        let mut b = Backoff::new(Duration::from_secs(60));
        let quick = Duration::from_millis(200);
        let waits: Vec<u64> = (0..7).map(|_| b.after_crash(quick).as_secs()).collect();
        assert_eq!(waits, [1, 2, 5, 10, 30, 30, 30]);
        assert_eq!(b.after_crash(Duration::from_secs(61)).as_secs(), 1);
        assert_eq!(b.after_crash(quick).as_secs(), 2);
        b.reset();
        assert_eq!(b.after_crash(quick).as_secs(), 1);
    }
}
