//! Estado real del proceso de cada servicio: `logs\status-<Servicio>.json` (lo escribe `vmsctl run`, lo lee
//! `vmsctl health wait`).
//!
//! El SCM solo sabe que `vmshost` está en marcha (y `vmshost` no sale nunca), así que no ve un hijo en bucle
//! de caídas. `vmsctl run` publica aquí qué hace de verdad: en marcha desde cuándo, esperando la
//! configuración, relanzando tras una caída… y las caídas de los últimos 10 min. Se reescribe en cada cambio
//! y cada [`REFRESH`] (para que un `vmsctl` muerto se note: [`STALE_AFTER`]).
//!
//! Regla de salud ([`evaluate`]): `running` desde hace al menos [`MIN_UPTIME`] (o [`STABLE_UPTIME`] si cayó en
//! los últimos 10 min: el mismo umbral de «estable» que la espera creciente), o `idle` (el servicio no tiene
//! nada que hacer en este puesto, p. ej. el latido sin panel central configurado).

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::time::Duration;
use vms_common::layout::DataLayout;
use vms_common::state::now_unix;

pub const SCHEMA: u32 = 1;
/// Ventana de caídas que se conserva (la misma de la versión a prueba).
pub const CRASH_WINDOW_S: u64 = 600;
/// Cada cuánto se reescribe aunque no cambie nada.
pub const REFRESH: Duration = Duration::from_secs(15);
/// Sin reescribir en este tiempo: el `vmsctl` que lo escribía ya no está.
pub const STALE_AFTER_S: u64 = 60;
/// Tiempo en marcha para darlo por sano…
pub const MIN_UPTIME_S: u64 = 20;
/// …o este, si cayó en los últimos 10 min (`vms_common::supervise::STABLE_AFTER`).
pub const STABLE_UPTIME_S: u64 = 60;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RunState {
    /// Esperando un archivo que crea otro (el YAML del motor).
    Waiting,
    /// Sin nada que hacer en este puesto (configuración opcional ausente): sano.
    Idle,
    Running,
    /// Cayó; se relanza tras la espera.
    Backoff,
    /// No se puede lanzar (falta el programa) o la versión a prueba sale tras cada caída.
    Failed,
    Stopped,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunStatus {
    pub schema: u32,
    pub service: String,
    /// Versión del `vmsctl` que lo escribe (las caídas de otra versión no cuentan).
    #[serde(default)]
    pub version: Option<String>,
    pub state: RunState,
    #[serde(default)]
    pub detail: String,
    pub vmsctl_pid: u32,
    #[serde(default)]
    pub child_pid: Option<u32>,
    /// Arranque del hijo actual (segundos Unix).
    #[serde(default)]
    pub started_unix: Option<u64>,
    #[serde(default)]
    pub crashes_unix: Vec<u64>,
    pub updated_unix: u64,
}

pub fn path_for(data: &DataLayout, service: &str) -> PathBuf {
    data.logs_dir().join(format!("status-{service}.json"))
}

pub fn read(path: &Path) -> Option<RunStatus> {
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

/// Escritor que usa `vmsctl run`.
pub struct Publisher {
    path: PathBuf,
    pub status: RunStatus,
    last_write: Option<std::time::Instant>,
    write_failed: bool,
}

impl Publisher {
    /// Conserva las caídas recientes del estado anterior si era de la misma versión (con `--exit-on-crash`
    /// cada caída es un `vmsctl` nuevo).
    pub fn new(path: PathBuf, service: &str, version: Option<&str>) -> Self {
        let now = now_unix();
        let crashes = read(&path)
            .filter(|old| old.version.as_deref() == version && old.service == service)
            .map(|old| old.crashes_unix.into_iter().filter(|t| now.saturating_sub(*t) < CRASH_WINDOW_S).collect())
            .unwrap_or_default();
        Self {
            path,
            status: RunStatus {
                schema: SCHEMA,
                service: service.to_string(),
                version: version.map(str::to_string),
                state: RunState::Waiting,
                detail: String::new(),
                vmsctl_pid: std::process::id(),
                child_pid: None,
                started_unix: None,
                crashes_unix: crashes,
                updated_unix: now,
            },
            last_write: None,
            write_failed: false,
        }
    }

    /// Cambia el estado y lo escribe. Devuelve un mensaje la primera vez que falla la escritura.
    pub fn set(&mut self, state: RunState, detail: &str, child_pid: Option<u32>) -> Option<String> {
        if state == RunState::Running && self.status.state != RunState::Running {
            self.status.started_unix = Some(now_unix());
        }
        if state != RunState::Running {
            self.status.started_unix = None;
        }
        self.status.state = state;
        self.status.detail = detail.to_string();
        self.status.child_pid = child_pid;
        self.write()
    }

    pub fn crashed(&mut self, detail: &str, state: RunState) -> Option<String> {
        let now = now_unix();
        self.status.crashes_unix.retain(|t| now.saturating_sub(*t) < CRASH_WINDOW_S);
        self.status.crashes_unix.push(now);
        self.set(state, detail, None)
    }

    /// Reescribe si toca ([`REFRESH`]).
    pub fn refresh(&mut self) -> Option<String> {
        if self.last_write.is_some_and(|t| t.elapsed() < REFRESH) {
            return None;
        }
        self.write()
    }

    fn write(&mut self) -> Option<String> {
        self.status.updated_unix = now_unix();
        self.last_write = Some(std::time::Instant::now());
        let mut data = serde_json::to_vec_pretty(&self.status).unwrap_or_default();
        data.push(b'\n');
        match vms_common::atomic_write(&self.path, &data) {
            Ok(()) => {
                self.write_failed = false;
                None
            }
            Err(e) if !self.write_failed => {
                self.write_failed = true;
                Some(format!("no se pudo escribir {}: {e}", self.path.display()))
            }
            Err(_) => None,
        }
    }
}

/// ¿Sano? (y por qué). `now`: segundos Unix.
pub fn evaluate(st: Option<&RunStatus>, now: u64) -> (bool, String) {
    let Some(st) = st else {
        return (false, "vmsctl run aún no publicó su estado".into());
    };
    let age = now.saturating_sub(st.updated_unix);
    if st.state != RunState::Stopped && age > STALE_AFTER_S {
        return (false, format!("estado sin actualizar desde hace {age} s (¿vmsctl run parado?)"));
    }
    let recent = st.crashes_unix.iter().filter(|t| now.saturating_sub(**t) < CRASH_WINDOW_S).count();
    let crashes = if recent > 0 { format!("; {recent} caídas en 10 min") } else { String::new() };
    match st.state {
        RunState::Idle => (true, format!("en espera: {}", st.detail)),
        RunState::Running => {
            let up = st.started_unix.map(|t| now.saturating_sub(t)).unwrap_or(0);
            let need = if recent > 0 { STABLE_UPTIME_S } else { MIN_UPTIME_S };
            if up >= need {
                (true, format!("en marcha desde hace {up} s{crashes}"))
            } else {
                (false, format!("en marcha desde hace solo {up} s (hacen falta {need}){crashes}"))
            }
        }
        RunState::Waiting => (false, format!("esperando: {}{crashes}", st.detail)),
        RunState::Backoff => (false, format!("cayó y se relanza: {}{crashes}", st.detail)),
        RunState::Failed => (false, format!("no arranca: {}{crashes}", st.detail)),
        RunState::Stopped => (false, "parado".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn st(state: RunState, started_ago: Option<u64>, crashes_ago: &[u64], now: u64) -> RunStatus {
        RunStatus {
            schema: SCHEMA,
            service: "VMSAnalytics".into(),
            version: Some("2.0.0".into()),
            state,
            detail: "x".into(),
            vmsctl_pid: 1,
            child_pid: Some(2),
            started_unix: started_ago.map(|a| now - a),
            crashes_unix: crashes_ago.iter().map(|a| now - a).collect(),
            updated_unix: now - 1,
        }
    }

    #[test]
    fn crash_loop_is_never_healthy() {
        let now = 10_000;
        // Recién relanzado tras caídas: ni a los 20 s
        assert!(!evaluate(Some(&st(RunState::Running, Some(25), &[30, 90, 200], now)), now).0);
        assert!(!evaluate(Some(&st(RunState::Backoff, None, &[1], now)), now).0);
        assert!(!evaluate(Some(&st(RunState::Failed, None, &[], now)), now).0);
        assert!(!evaluate(None, now).0);
        // Estable un minuto tras la última caída: sano
        let (ok, why) = evaluate(Some(&st(RunState::Running, Some(61), &[70], now)), now);
        assert!(ok, "{why}");
        assert!(why.contains("1 caídas"), "{why}");
    }

    #[test]
    fn fresh_start_needs_twenty_seconds_and_idle_is_fine() {
        let now = 10_000;
        assert!(!evaluate(Some(&st(RunState::Running, Some(5), &[], now)), now).0);
        assert!(evaluate(Some(&st(RunState::Running, Some(20), &[], now)), now).0);
        assert!(evaluate(Some(&st(RunState::Idle, None, &[], now)), now).0);
        assert!(!evaluate(Some(&st(RunState::Waiting, None, &[], now)), now).0);
        // Caídas de hace más de 10 min ya no cuentan
        assert!(evaluate(Some(&st(RunState::Running, Some(30), &[700], now)), now).0);
    }

    #[test]
    fn stale_status_means_vmsctl_is_gone() {
        let now = 10_000;
        let mut s = st(RunState::Running, Some(500), &[], now);
        s.updated_unix = now - STALE_AFTER_S - 1;
        let (ok, why) = evaluate(Some(&s), now);
        assert!(!ok && why.contains("sin actualizar"), "{why}");
    }

    #[test]
    fn publisher_keeps_recent_crashes_of_the_same_version_only() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("logs").join("status-VMSBackend.json");
        let mut a = Publisher::new(p.clone(), "VMSBackend", Some("2.1.0"));
        a.set(RunState::Running, "", Some(9));
        a.crashed("código 1", RunState::Failed);
        let back = read(&p).unwrap();
        assert_eq!((back.state, back.crashes_unix.len(), back.started_unix), (RunState::Failed, 1, None));
        // Siguiente vmsctl de la misma versión (modo a prueba): conserva la caída
        assert_eq!(Publisher::new(p.clone(), "VMSBackend", Some("2.1.0")).status.crashes_unix.len(), 1);
        // De otra versión (tras una vuelta atrás): empieza de cero
        assert!(Publisher::new(p, "VMSBackend", Some("2.0.0")).status.crashes_unix.is_empty());
    }
}
