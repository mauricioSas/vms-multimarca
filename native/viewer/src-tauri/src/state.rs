//! Estado compartido del visor (independiente de Tauri, para poder probarlo).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, RwLock};
use std::time::Instant;

use crate::config::{LoadOutcome, ViewerConfig};
use crate::health::{Health, Level};
use crate::kiosk::Token;
use crate::paths::{self, InstallLayout};
use crate::updates::UpdateView;

pub const PANEL: &str = "panel";

pub fn wall_label(n: u8) -> String {
    format!("muro-{n}")
}

pub fn wall_of(label: &str) -> Option<u8> {
    label.strip_prefix("muro-").and_then(|n| n.parse().ok()).filter(|n| (1..=4).contains(n))
}

/// Qué tiene que mostrar una ventana del panel o de un muro cuando el servidor está disponible.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Target {
    pub server: String,
    pub path: String,
}

/// Lo que consulta el filtro de navegación de cada ventana (se llama desde el hilo de la interfaz: nada lento).
#[derive(Debug, Default)]
pub struct NavState {
    pub allowed_origins: Vec<String>,
    /// Ventanas a las que el visor acaba de mandar a una página local (permiso de un solo uso).
    pub expect_local: HashMap<String, bool>,
    /// Ventanas que ahora mismo muestran una página local.
    pub on_local: HashMap<String, bool>,
}

/// Intercambio del token de kiosco pendiente: se hace al terminar de cargar `/api/local/kiosk`.
pub struct PendingKiosk {
    pub origin: String,
    pub next: String,
    pub token: Token,
    pub created: Instant,
}

/// Datos de la página de certificado de una ventana.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CertView {
    pub server: String,
    pub url: String,
    pub expected: Option<String>,
    pub observed: String,
}

pub struct Viewer {
    /// Versión del producto con la que se instaló este visor: la de su carpeta `versions\<X>`, la misma que usan el
    /// puntero, el actualizador y `installed` de `public-status.json`. Solo una copia sin instalar (desarrollo) usa
    /// la del Cargo.toml, que en todas las versiones publicadas es `2.0.0-dev.0` y no dice nada del producto.
    pub version: String,
    pub data_dir: PathBuf,
    pub cfg_path: PathBuf,
    pub cfg: Mutex<ViewerConfig>,
    pub layout: Option<InstallLayout>,
    pub targets: Mutex<HashMap<String, Target>>,
    pub nav: Arc<RwLock<NavState>>,
    /// `(origen, huella)` de los servidores remotos, para el manejador de certificados de WebView2.
    pub pins: Arc<RwLock<Vec<(String, String)>>>,
    pub pending_kiosk: Mutex<HashMap<String, PendingKiosk>>,
    pub kiosk_attempts: Mutex<HashMap<String, Instant>>,
    /// Última huella observada por origen (`probar_servidor` y el aviso de certificado).
    pub observed: Mutex<HashMap<String, String>>,
    pub cert_views: Mutex<HashMap<String, CertView>>,
    /// Motivo de «sin permiso» por ventana (`TokenError::code` o el del servidor).
    pub denied: Mutex<HashMap<String, String>>,
    pub walls_open: Mutex<BTreeSet<u8>>,
    /// Clave del monitor en el que está colocado cada muro (para no moverlo si no cambia).
    pub placed: Mutex<HashMap<u8, String>>,
    pub health: Mutex<Health>,
    pub update: Mutex<UpdateView>,
    /// Versión instalada ya comparada con el propio ejecutable: (versión, ¿hay que reiniciar?).
    pub restart_check: Mutex<Option<(String, bool)>>,
    pub panel_notice_shown: Mutex<Option<String>>,
    /// Versión nueva publicada en GitHub (comprobación del visor, `github_update`) y si ya se avisó de ella.
    pub novedad: Mutex<Option<crate::github_update::Novedad>>,
    pub novedad_avisada: Mutex<Option<String>>,
    /// Ventanas con una comprobación de servidor en curso.
    pub going: Mutex<HashSet<String>>,
}

impl Viewer {
    /// `build_version`: la del Cargo.toml (`CARGO_PKG_VERSION`); solo se usa si el visor no está instalado.
    pub fn new(
        build_version: &str,
        data_dir: PathBuf,
        cfg_path: PathBuf,
        layout: Option<InstallLayout>,
    ) -> (Self, LoadOutcome) {
        let (cfg, outcome) = ViewerConfig::load(&cfg_path);
        let version = layout.as_ref().map(|l| l.version.clone()).unwrap_or_else(|| build_version.to_string());
        let v = Self {
            version: version.clone(),
            data_dir,
            cfg_path,
            cfg: Mutex::new(cfg),
            layout,
            targets: Mutex::new(HashMap::new()),
            nav: Arc::new(RwLock::new(NavState::default())),
            pins: Arc::new(RwLock::new(Vec::new())),
            pending_kiosk: Mutex::new(HashMap::new()),
            kiosk_attempts: Mutex::new(HashMap::new()),
            observed: Mutex::new(HashMap::new()),
            cert_views: Mutex::new(HashMap::new()),
            denied: Mutex::new(HashMap::new()),
            walls_open: Mutex::new(BTreeSet::new()),
            placed: Mutex::new(HashMap::new()),
            health: Mutex::new(Health { level: Level::Unknown, version: None }),
            update: Mutex::new(UpdateView { label: format!("Versión {version}"), restart_into: None }),
            restart_check: Mutex::new(None),
            panel_notice_shown: Mutex::new(None),
            novedad: Mutex::new(None),
            novedad_avisada: Mutex::new(None),
            going: Mutex::new(HashSet::new()),
        };
        v.refresh_policies();
        (v, outcome)
    }

    pub fn for_current_user(build_version: &str) -> (Self, LoadOutcome) {
        let layout = std::env::current_exe().ok().and_then(|e| InstallLayout::from_exe(&e));
        Self::new(build_version, paths::data_dir(), paths::viewer_config_file(), layout)
    }

    pub fn config(&self) -> ViewerConfig {
        self.cfg.lock().map(|c| c.clone()).unwrap_or_default()
    }

    /// Cambia la configuración, la guarda y recalcula la navegación permitida y las huellas.
    pub fn update_config(&self, f: impl FnOnce(&mut ViewerConfig) -> Result<(), String>) -> Result<(), String> {
        {
            let mut cfg = self.cfg.lock().map_err(|_| "estado bloqueado".to_string())?;
            let mut next = cfg.clone();
            f(&mut next)?;
            next.sanitize();
            next.save(&self.cfg_path).map_err(|e| format!("No se pudo guardar {}: {e}", self.cfg_path.display()))?;
            *cfg = next;
        }
        self.refresh_policies();
        Ok(())
    }

    pub fn refresh_policies(&self) {
        let servers = self.config().all_servers();
        if let Ok(mut nav) = self.nav.write() {
            nav.allowed_origins = servers.iter().map(|s| s.url.origin()).collect();
        }
        if let Ok(mut pins) = self.pins.write() {
            *pins = servers.iter().filter_map(|s| Some((s.url.origin(), s.sha256.clone()?))).collect();
        }
    }

    pub fn set_target(&self, label: &str, server: &str, path: &str) {
        if let Ok(mut t) = self.targets.lock() {
            t.insert(label.to_string(), Target { server: server.to_string(), path: path.to_string() });
        }
    }

    pub fn target(&self, label: &str) -> Option<Target> {
        self.targets.lock().ok()?.get(label).cloned()
    }

    pub fn expect_local(&self, label: &str) {
        if let Ok(mut nav) = self.nav.write() {
            nav.expect_local.insert(label.to_string(), true);
        }
    }

    pub fn kiosk_token_path(&self) -> PathBuf {
        paths::kiosk_token_file(&self.data_dir)
    }

    pub fn public_status_path(&self) -> PathBuf {
        paths::public_status_file(&self.data_dir)
    }
}

/// Decisión del filtro de navegación de una ventana.
pub fn navigation_decision(nav: &NavState, label: &str, url: &tauri::Url) -> bool {
    if crate::servers::is_local_url(url) {
        return nav.expect_local.get(label).copied().unwrap_or(false)
            || nav.on_local.get(label).copied().unwrap_or(false);
    }
    crate::servers::navigation_allowed(url, &nav.allowed_origins)
}

/// Qué hace el visor cuando una página pide una ventana nueva (`<a target="…">`, `window.open`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NewWindow {
    /// Abrir (o traer delante) la ventana del muro N del propio visor, como «Muros → Muro N» de la bandeja.
    Wall(u8),
    /// Nada: el visor no abre ventanas de navegador sueltas.
    Deny,
}

/// «Abrir muro» y «Monitor N» del panel son enlaces a `/wall/N` con `target="vms-wall-N"`. En un navegador abren
/// una pestaña; dentro del visor, el muro N del propio visor. Solo desde el panel y solo si la dirección es la
/// del servidor de ese muro (si no, la ventana del visor enseñaría otro muro). Todo lo demás se rechaza.
pub fn new_window_decision(opener: &str, url: &tauri::Url, cfg: &ViewerConfig) -> NewWindow {
    if opener != PANEL {
        return NewWindow::Deny;
    }
    let Some(n) = wall_path(url.path()) else { return NewWindow::Deny };
    let server = cfg.wall(n).map(|w| w.server.as_str()).unwrap_or(crate::servers::LOCAL_SERVER);
    match (cfg.resolve(server), crate::servers::origin_of(url)) {
        (Ok(s), Some(origin)) if s.url.origin() == origin => NewWindow::Wall(n),
        _ => NewWindow::Deny,
    }
}

/// `/wall/N` (1-4, con o sin barra final) → N.
fn wall_path(path: &str) -> Option<u8> {
    let rest = path.strip_prefix("/wall/")?;
    match rest.strip_suffix('/').unwrap_or(rest) {
        "1" => Some(1),
        "2" => Some(2),
        "3" => Some(3),
        "4" => Some(4),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn viewer() -> (tempfile::TempDir, Viewer) {
        let dir = tempfile::tempdir().unwrap();
        let (v, _) = Viewer::new("2.0.0", dir.path().join("datos"), dir.path().join("cfg/viewer.json"), None);
        (dir, v)
    }

    /// En el PC del usuario, «Diagnóstico» decía «Versión del visor 2.0.0-dev.0» y «Se volvió a 2.0.0-beta.5:
    /// reiniciar el visor» con el visor de la beta.5: comparaba la versión del Cargo.toml con la instalada.
    #[test]
    fn the_viewer_reports_the_product_version_it_was_installed_with() {
        let dir = tempfile::tempdir().unwrap();
        let exe = dir.path().join("versions").join("2.0.0-beta.5").join("viewer").join("VMS.exe");
        let layout = InstallLayout::from_exe(&exe).expect("disposición instalada");
        let (v, _) = Viewer::new("2.0.0-dev.0", dir.path().join("datos"), dir.path().join("viewer.json"), Some(layout));
        assert_eq!(v.version, "2.0.0-beta.5");
        let st = crate::updates::parse(
            br#"{"installed": "2.0.0-beta.5", "channel": "stable", "state": "idle", "last_result": "error"}"#,
        )
        .unwrap();
        let view = crate::updates::view(&v.version, Some(&st));
        assert_eq!(view.restart_into, None);
        assert!(!view.label.contains("reiniciar"), "{}", view.label);
        // una copia sin instalar (desarrollo) sigue con la del Cargo.toml
        let (dev, _) = Viewer::new("2.0.0-dev.0", dir.path().join("datos"), dir.path().join("viewer.json"), None);
        assert_eq!(dev.version, "2.0.0-dev.0");
    }

    #[test]
    fn labels() {
        assert_eq!(wall_label(3), "muro-3");
        assert_eq!(wall_of("muro-3"), Some(3));
        assert_eq!(wall_of("muro-9"), None);
        assert_eq!(wall_of("panel"), None);
    }

    #[test]
    fn remote_pages_cannot_open_local_pages_on_their_own() {
        let (_d, v) = viewer();
        let local = crate::servers::local_page("servidores.html", &[]);
        let backend = tauri::Url::parse("http://127.0.0.1:8600/wall/1").unwrap();
        {
            let nav = v.nav.read().unwrap();
            assert!(navigation_decision(&nav, "muro-1", &backend));
            assert!(!navigation_decision(&nav, "muro-1", &local), "una página del backend no abre páginas locales");
        }
        v.expect_local("muro-1");
        assert!(navigation_decision(&v.nav.read().unwrap(), "muro-1", &local));
        assert!(!navigation_decision(&v.nav.read().unwrap(), "muro-2", &local));
    }

    /// «Abrir muro» del panel no hacía nada dentro del visor (toda ventana nueva se rechazaba): ahora abre el
    /// muro N del propio visor, y solo eso.
    #[test]
    fn open_wall_from_the_panel_opens_the_viewers_own_wall() {
        let decide = |opener: &str, url: &str, cfg: &ViewerConfig| {
            new_window_decision(opener, &tauri::Url::parse(url).unwrap(), cfg)
        };
        let mut cfg = ViewerConfig::default();
        for n in 1..=4u8 {
            let url = format!("http://127.0.0.1:8600/wall/{n}");
            assert_eq!(decide(PANEL, &url, &cfg), NewWindow::Wall(n), "{url}");
        }
        assert_eq!(decide(PANEL, "http://127.0.0.1:8600/wall/2/", &cfg), NewWindow::Wall(2));
        assert_eq!(decide(PANEL, "http://127.0.0.1:8600/wall/2?x=1#y", &cfg), NewWindow::Wall(2));
        for other in [
            "http://127.0.0.1:8600/wall/0",
            "http://127.0.0.1:8600/wall/5",
            "http://127.0.0.1:8600/wall/01",
            "http://127.0.0.1:8600/wall/+1",
            "http://127.0.0.1:8600/wall/1/x",
            "http://127.0.0.1:8600/wall",
            "http://127.0.0.1:8600/status",
            "http://127.0.0.1:8601/wall/1",
            "https://127.0.0.1:8600/wall/1",
            "https://evil.example/wall/1",
            "http://tauri.localhost/wall/1",
            "javascript:alert(1)",
            "about:blank",
        ] {
            assert_eq!(decide(PANEL, other, &cfg), NewWindow::Deny, "{other}");
        }
        // solo desde el panel: un muro o una página local no abren ventanas
        for opener in ["muro-1", "servidores", "diagnostico"] {
            assert_eq!(decide(opener, "http://127.0.0.1:8600/wall/1", &cfg), NewWindow::Deny, "{opener}");
        }
        // un muro asignado a otro servidor solo se abre con la dirección de ese servidor
        cfg.upsert_server("Central", "https://central:8643", Some(&"c".repeat(64))).unwrap();
        cfg.wall_mut(3).server = "Central".into();
        assert_eq!(decide(PANEL, "https://central:8643/wall/3", &cfg), NewWindow::Wall(3));
        assert_eq!(decide(PANEL, "http://127.0.0.1:8600/wall/3", &cfg), NewWindow::Deny);
        assert_eq!(decide(PANEL, "https://central:8643/wall/1", &cfg), NewWindow::Deny);
    }

    #[test]
    fn config_changes_update_navigation_and_pins() {
        let (_d, v) = viewer();
        let fp = "c".repeat(64);
        v.update_config(|c| c.upsert_server("central", "https://central:8643", Some(&fp))).unwrap();
        assert!(v.nav.read().unwrap().allowed_origins.contains(&"https://central:8643".to_string()));
        assert_eq!(v.pins.read().unwrap().as_slice(), &[("https://central:8643".to_string(), fp)]);
        assert!(v.cfg_path.is_file(), "se guardó");
        // un cambio inválido no se guarda ni rompe lo anterior
        assert!(v.update_config(|c| c.upsert_server("x", "http://10.0.0.1:8600", None)).is_err());
        assert_eq!(v.config().servers.len(), 1);
    }
}
