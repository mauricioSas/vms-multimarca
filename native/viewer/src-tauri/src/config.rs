//! `%APPDATA%\VMSMultimarca\viewer.json` (CONTRATO §17.2):
//!
//! ```json
//! {"schema": 1,
//!  "servers": [{"name": "central", "url": "https://central:8643", "sha256": "ab12…"}],
//!  "walls": [{"wall": 1, "server": "local", "monitor_key": "\\\\.\\DISPLAY2|1920,0|1920x1080"}],
//!  "panel": "local"}
//! ```
//!
//! - El servidor `local` (`http://127.0.0.1:8600`) existe siempre aunque no esté en la lista.
//! - Se guarda con `vms_common::atomic_write`. Los campos que esta versión no conoce se conservan.
//! - Un archivo corrupto no impide arrancar: se aparta como `viewer.json.corrupto-<unix>` y se empieza de cero.

use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::pinning;
use crate::servers::{parse_server_url, ServerUrl, DEFAULT_LOCAL_URL, LOCAL_SERVER};

pub const SCHEMA: u32 = 1;
pub const MAX_WALLS: u8 = 4;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Server {
    pub name: String,
    pub url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sha256: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct WallAssignment {
    pub wall: u8,
    #[serde(default = "local_name")]
    pub server: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub monitor_key: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

fn local_name() -> String {
    LOCAL_SERVER.to_string()
}

fn schema_default() -> u32 {
    SCHEMA
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ViewerConfig {
    #[serde(default = "schema_default")]
    pub schema: u32,
    #[serde(default)]
    pub servers: Vec<Server>,
    #[serde(default)]
    pub walls: Vec<WallAssignment>,
    /// Servidor que abre el «Panel» (y cuyo estado pinta la bandeja). Por defecto `local`.
    #[serde(default = "local_name")]
    pub panel: String,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

impl Default for ViewerConfig {
    fn default() -> Self {
        Self { schema: SCHEMA, servers: Vec::new(), walls: Vec::new(), panel: local_name(), extra: BTreeMap::new() }
    }
}

/// Servidor ya validado, listo para conectar.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedServer {
    pub name: String,
    pub url: ServerUrl,
    pub sha256: Option<String>,
}

#[derive(Debug)]
pub enum LoadOutcome {
    Loaded,
    Missing,
    /// El archivo no se podía leer como JSON válido: se apartó a esta ruta.
    Corrupt(PathBuf),
}

pub fn valid_server_name(name: &str) -> bool {
    let n = name.trim();
    !n.is_empty() && n.chars().count() <= 64 && !n.chars().any(|c| c.is_control())
}

impl ViewerConfig {
    pub fn load(path: &Path) -> (Self, LoadOutcome) {
        let bytes = match std::fs::read(path) {
            Ok(b) => b,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return (Self::default(), LoadOutcome::Missing),
            Err(_) => return (Self::default(), LoadOutcome::Missing),
        };
        match serde_json::from_slice::<ViewerConfig>(&bytes) {
            Ok(mut cfg) => {
                cfg.sanitize();
                (cfg, LoadOutcome::Loaded)
            }
            Err(_) => {
                let secs = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let aside = path.with_file_name(format!("viewer.json.corrupto-{secs}"));
                let _ = std::fs::rename(path, &aside);
                (Self::default(), LoadOutcome::Corrupt(aside))
            }
        }
    }

    pub fn save(&self, path: &Path) -> io::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut text = serde_json::to_vec_pretty(self).map_err(io::Error::other)?;
        text.push(b'\n');
        vms_common::atomic_write(path, &text)
    }

    /// Quita lo que no se puede usar (muros fuera de 1-4 o repetidos, servidores sin nombre o duplicados) sin
    /// tocar los campos desconocidos.
    pub fn sanitize(&mut self) {
        let mut seen = std::collections::HashSet::new();
        self.walls.retain(|w| (1..=MAX_WALLS).contains(&w.wall) && seen.insert(w.wall));
        self.walls.sort_by_key(|w| w.wall);
        let mut names = std::collections::HashSet::new();
        self.servers.retain(|s| valid_server_name(&s.name) && names.insert(s.name.trim().to_lowercase()));
        for s in &mut self.servers {
            s.sha256 = s.sha256.as_deref().and_then(pinning::normalize_fingerprint);
        }
        if self.panel.trim().is_empty() {
            self.panel = local_name();
        }
    }

    /// Servidor por nombre (sin distinguir mayúsculas). `local` existe siempre.
    pub fn resolve(&self, name: &str) -> Result<ResolvedServer, String> {
        if let Some(s) = self.servers.iter().find(|s| s.name.eq_ignore_ascii_case(name)) {
            let url = parse_server_url(&s.url).map_err(|e| format!("Servidor «{}»: {e}", s.name))?;
            return Ok(ResolvedServer { name: s.name.clone(), url, sha256: s.sha256.clone() });
        }
        if name.eq_ignore_ascii_case(LOCAL_SERVER) {
            let url = parse_server_url(DEFAULT_LOCAL_URL).expect("URL local por defecto válida");
            return Ok(ResolvedServer { name: LOCAL_SERVER.into(), url, sha256: None });
        }
        Err(format!("No hay ningún servidor llamado «{name}» en la lista de servidores"))
    }

    /// Todos los servidores utilizables (incluido `local`), para la lista de navegación permitida.
    pub fn all_servers(&self) -> Vec<ResolvedServer> {
        let mut out: Vec<ResolvedServer> = self.servers.iter().filter_map(|s| self.resolve(&s.name).ok()).collect();
        if !out.iter().any(|s| s.name.eq_ignore_ascii_case(LOCAL_SERVER)) {
            if let Ok(l) = self.resolve(LOCAL_SERVER) {
                out.insert(0, l);
            }
        }
        out
    }

    pub fn wall(&self, wall: u8) -> Option<&WallAssignment> {
        self.walls.iter().find(|w| w.wall == wall)
    }

    pub fn wall_mut(&mut self, wall: u8) -> &mut WallAssignment {
        if let Some(i) = self.walls.iter().position(|w| w.wall == wall) {
            return &mut self.walls[i];
        }
        self.walls.push(WallAssignment { wall, server: local_name(), monitor_key: None, extra: BTreeMap::new() });
        self.walls.sort_by_key(|w| w.wall);
        let i = self.walls.iter().position(|w| w.wall == wall).expect("recién añadido");
        &mut self.walls[i]
    }

    /// Añade o sustituye un servidor (validado). Un servidor remoto exige su huella.
    pub fn upsert_server(&mut self, name: &str, url: &str, sha256: Option<&str>) -> Result<(), String> {
        let name = name.trim();
        if !valid_server_name(name) {
            return Err("El nombre tiene que tener entre 1 y 64 caracteres".into());
        }
        let parsed = parse_server_url(url)?;
        let pin = match sha256 {
            Some(s) => Some(pinning::normalize_fingerprint(s).ok_or("La huella SHA-256 no es válida")?),
            None => None,
        };
        if parsed.https && pin.is_none() {
            return Err("Falta confirmar la huella del certificado del servidor".into());
        }
        let entry = Server { name: name.into(), url: parsed.origin(), sha256: pin, extra: BTreeMap::new() };
        match self.servers.iter_mut().find(|s| s.name.eq_ignore_ascii_case(name)) {
            Some(s) => {
                s.url = entry.url;
                s.sha256 = entry.sha256;
            }
            None => self.servers.push(entry),
        }
        Ok(())
    }

    pub fn remove_server(&mut self, name: &str) -> Result<(), String> {
        if name.eq_ignore_ascii_case(LOCAL_SERVER) {
            return Err("El servidor local no se puede borrar".into());
        }
        let before = self.servers.len();
        self.servers.retain(|s| !s.name.eq_ignore_ascii_case(name));
        if self.servers.len() == before {
            return Err(format!("No hay ningún servidor llamado «{name}»"));
        }
        for w in &mut self.walls {
            if w.server.eq_ignore_ascii_case(name) {
                w.server = local_name();
            }
        }
        if self.panel.eq_ignore_ascii_case(name) {
            self.panel = local_name();
        }
        Ok(())
    }

    pub fn set_pin(&mut self, name: &str, sha256: &str) -> Result<(), String> {
        let pin = pinning::normalize_fingerprint(sha256).ok_or("La huella SHA-256 no es válida")?;
        let s = self
            .servers
            .iter_mut()
            .find(|s| s.name.eq_ignore_ascii_case(name))
            .ok_or_else(|| format!("No hay ningún servidor llamado «{name}»"))?;
        s.sha256 = Some(pin);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FP: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn contract_example_round_trips_and_keeps_unknown_fields() {
        let text = format!(
            r#"{{"schema": 1, "servers": [{{"name": "central", "url": "https://central:8643", "sha256": "{FP}", "nota": "x"}}],
                "walls": [{{"wall": 2, "server": "central", "monitor_key": "M|1920,0|1920x1080", "futuro": true}}],
                "otra_cosa": [1, 2]}}"#
        );
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("viewer.json");
        std::fs::write(&path, text).unwrap();
        let (cfg, outcome) = ViewerConfig::load(&path);
        assert!(matches!(outcome, LoadOutcome::Loaded));
        assert_eq!(cfg.panel, "local");
        assert_eq!(cfg.walls[0].monitor_key.as_deref(), Some("M|1920,0|1920x1080"));
        cfg.save(&path).unwrap();
        let back: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(back["otra_cosa"], serde_json::json!([1, 2]));
        assert_eq!(back["walls"][0]["futuro"], Value::Bool(true));
        assert_eq!(back["servers"][0]["nota"], "x");
        assert_eq!(back["servers"][0]["sha256"], FP);
    }

    #[test]
    fn missing_and_corrupt_files_do_not_block_startup() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("viewer.json");
        let (cfg, outcome) = ViewerConfig::load(&path);
        assert!(matches!(outcome, LoadOutcome::Missing));
        assert_eq!(cfg, ViewerConfig::default());
        std::fs::write(&path, b"{ esto no es json").unwrap();
        let (cfg, outcome) = ViewerConfig::load(&path);
        assert_eq!(cfg, ViewerConfig::default());
        match outcome {
            LoadOutcome::Corrupt(aside) => assert!(aside.is_file()),
            other => panic!("esperaba Corrupt, no {other:?}"),
        }
        assert!(!path.exists());
    }

    #[test]
    fn local_server_always_exists_and_remote_needs_pin() {
        let mut cfg = ViewerConfig::default();
        assert_eq!(cfg.resolve("local").unwrap().url.origin(), "http://127.0.0.1:8600");
        assert!(cfg.resolve("central").is_err());
        assert!(cfg.upsert_server("central", "https://central:8643", None).is_err());
        assert!(cfg.upsert_server("central", "https://central:8643", Some("zz")).is_err());
        let colons = FP.as_bytes().chunks(2).map(|c| std::str::from_utf8(c).unwrap()).collect::<Vec<_>>().join(":");
        cfg.upsert_server("central", "central:8643", Some(&colons.to_uppercase())).unwrap();
        let r = cfg.resolve("CENTRAL").unwrap();
        assert_eq!(r.sha256.as_deref(), Some(FP));
        assert_eq!(r.url.origin(), "https://central:8643");
        // el local se puede redefinir (otro puerto) pero no borrar
        cfg.upsert_server("local", "http://127.0.0.1:8700", None).unwrap();
        assert_eq!(cfg.resolve("local").unwrap().url.port, 8700);
        assert!(cfg.remove_server("local").is_err());
        assert_eq!(cfg.all_servers().len(), 2);
    }

    #[test]
    fn removing_a_server_moves_its_walls_back_to_local() {
        let mut cfg = ViewerConfig::default();
        cfg.upsert_server("central", "https://central:8643", Some(FP)).unwrap();
        cfg.wall_mut(3).server = "central".into();
        cfg.panel = "central".into();
        cfg.remove_server("central").unwrap();
        assert_eq!(cfg.wall(3).unwrap().server, "local");
        assert_eq!(cfg.panel, "local");
    }

    #[test]
    fn sanitize_drops_invalid_walls_and_duplicates() {
        let mut cfg: ViewerConfig = serde_json::from_str(
            r#"{"walls": [{"wall": 0}, {"wall": 5}, {"wall": 2}, {"wall": 2, "server": "x"}, {"wall": 1}],
                "servers": [{"name": "", "url": "https://a:1"}, {"name": "A", "url": "https://a:1"},
                            {"name": "a", "url": "https://b:1"}]}"#,
        )
        .unwrap();
        cfg.sanitize();
        assert_eq!(cfg.walls.iter().map(|w| w.wall).collect::<Vec<_>>(), vec![1, 2]);
        assert_eq!(cfg.walls[1].server, "local");
        assert_eq!(cfg.servers.len(), 1);
    }
}
