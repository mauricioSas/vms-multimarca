//! La aplicación Tauri: ventanas (panel, muros por monitor, páginas locales), bandeja, instancia única y los
//! hilos que vigilan la salud del servicio, los monitores y las actualizaciones (PLAN-V2 §2.3 y §2.5).

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::{Duration, Instant};

use tauri::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};
use tauri::webview::{NewWindowResponse, PageLoadEvent, PageLoadPayload};
use tauri::{
    AppHandle, Manager, PhysicalPosition, PhysicalSize, RunEvent, Runtime, Url, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent,
};

use crate::config::ViewerConfig;
use crate::health::{self, Level};
use crate::monitors::{self, MonitorInfo};
use crate::pinning::{self, PinCheck};
use crate::servers::{self, origin_of};
use crate::state::{self, wall_label, wall_of, CertView, PendingKiosk, Viewer, PANEL};
use crate::{http, kiosk, log, platform, tray_icons, updates};

/// Comandos IPC (deben coincidir con `build.rs`; lo comprueba `tests/ipc_acl.rs`).
pub const COMMANDS: &[&str] = &[
    "reintentar",
    "diagnostico",
    "servidores",
    "probar_servidor",
    "guardar_servidor",
    "borrar_servidor",
    "asignar_servidor_muro",
    "certificado",
    "confiar_certificado",
    "monitores",
    "asignar_monitor",
    "acerca",
    "actualizaciones",
    "buscar_actualizaciones",
    "volver_version_anterior",
    "sin_permiso",
];

pub const TRAY_ID: &str = "vms";
const HEALTH_EVERY: Duration = Duration::from_secs(5);
const MONITORS_EVERY: Duration = Duration::from_secs(5);
const UPDATES_EVERY: Duration = Duration::from_secs(10);
const PANEL_IDLE_BEFORE_RESTART: Duration = Duration::from_secs(60);
const KIOSK_RETRY_GAP: Duration = Duration::from_secs(30);
const PROBE_TIMEOUT: Duration = Duration::from_secs(3);

/// Ventanas con página local propia (se abren desde la bandeja o con `--abrir <nombre>`).
pub const LOCAL_WINDOWS: &[(&str, &str, &str)] = &[
    ("servidores", "servidores.html", "Servidores"),
    ("monitores", "monitores.html", "Muros y monitores"),
    ("acerca", "acerca.html", "Acerca de"),
    ("diagnostico", "diagnostico.html", "Diagnóstico"),
    ("actualizaciones", "actualizaciones.html", "Actualizaciones"),
];

// ============================================================================================ argumentos
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Options {
    pub walls: bool,
    pub panel: bool,
    /// Arrancar solo en la bandeja (inicio con la sesión).
    pub tray_only: bool,
    pub open: Option<String>,
    pub after_pid: Option<u32>,
}

pub fn parse_args(args: &[String]) -> Options {
    let mut o = Options::default();
    let mut it = args.iter().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--walls" | "--muros" => o.walls = true,
            "--panel" => o.panel = true,
            "--bandeja" | "--tray" => o.tray_only = true,
            "--abrir" => o.open = it.next().filter(|n| LOCAL_WINDOWS.iter().any(|(l, ..)| l == n)).cloned(),
            "--after-pid" => o.after_pid = it.next().and_then(|p| p.parse().ok()),
            _ => {}
        }
    }
    if !o.walls && !o.tray_only && o.open.is_none() {
        o.panel = true;
    }
    o
}

// ============================================================================================ ventanas
fn browser_args() -> String {
    // Los mismos argumentos en todas las ventanas: comparten el entorno (y el proceso de GPU) de WebView2.
    let mut args = String::from(
        "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --autoplay-policy=no-user-gesture-required",
    );
    if let Some(port) = cdp_port() {
        args.push_str(&format!(" --remote-debugging-port={port}"));
    }
    args
}

/// Puerto CDP solo en builds de prueba (`--features prueba`), PLAN-V2 §4.6.
pub fn cdp_port() -> Option<u16> {
    if cfg!(feature = "prueba") {
        std::env::var("VMS_VIEWER_CDP_PORT").ok().and_then(|p| p.parse().ok())
    } else {
        None
    }
}

fn viewer<R: Runtime>(app: &AppHandle<R>) -> Arc<Viewer> {
    app.state::<Arc<Viewer>>().inner().clone()
}

/// Crea (o devuelve) la ventana `label`. Las de panel y muros arrancan en «Conectando…».
pub fn ensure_window<R: Runtime>(app: &AppHandle<R>, label: &str) -> tauri::Result<WebviewWindow<R>> {
    if let Some(w) = app.get_webview_window(label) {
        return Ok(w);
    }
    let v = viewer(app);
    let wall = wall_of(label);
    let local = LOCAL_WINDOWS.iter().find(|(l, ..)| *l == label);
    let (page, title) = match (wall, local) {
        (Some(n), _) => ("conectando.html".to_string(), format!("Muro {n} · VMS Multimarca")),
        (None, Some((_, page, title))) => (page.to_string(), format!("{title} · VMS Multimarca")),
        (None, None) => ("conectando.html".to_string(), "VMS Multimarca".to_string()),
    };
    v.expect_local(label);
    let nav = v.nav.clone();
    let nav_label = label.to_string();
    let nav_app = app.clone();
    let load_app = app.clone();
    let mut b = WebviewWindowBuilder::new(app, label, WebviewUrl::App(page.into()))
        .title(title)
        .additional_browser_args(&browser_args())
        .on_navigation(move |url| on_navigation(&nav_app, &nav, &nav_label, url))
        .on_page_load(move |w, p| on_page_load(&load_app, &w, &p))
        .on_new_window(|_, _| NewWindowResponse::Deny);
    b = match (wall, local) {
        (Some(_), _) => b.decorations(false).visible(false).skip_taskbar(true).inner_size(960.0, 540.0),
        (None, Some(_)) => b.inner_size(760.0, 640.0).min_inner_size(520.0, 420.0).center(),
        (None, None) => b.inner_size(1360.0, 860.0).min_inner_size(800.0, 560.0).center(),
    };
    let w = b.build()?;
    #[cfg(windows)]
    crate::webview2_cert::install(&w, v.pins.clone(), wall.is_some(), app.clone(), label.to_string());
    Ok(w)
}

fn on_navigation<R: Runtime>(
    app: &AppHandle<R>,
    nav: &std::sync::RwLock<state::NavState>,
    label: &str,
    url: &Url,
) -> bool {
    let allowed = nav.read().map(|n| state::navigation_decision(&n, label, url)).unwrap_or(false);
    if !allowed {
        log::warn(format!("Navegación bloqueada en «{label}»: {}", strip_query(url)));
        return false;
    }
    // Un muro del servidor local que cae en /login: la cookie de kiosco caducó o se rotó el token.
    if let Some(n) = wall_of(label) {
        if url.path() == "/login" && !servers::is_local_url(url) {
            let v = viewer(app);
            let target = v.target(label);
            let is_local_server = target
                .and_then(|t| v.config().resolve(&t.server).ok())
                .map(|s| s.url.is_loopback() && !s.url.https)
                .unwrap_or(false);
            if is_local_server {
                let recent = v
                    .kiosk_attempts
                    .lock()
                    .ok()
                    .and_then(|m| m.get(label).copied())
                    .map(|t| t.elapsed() < KIOSK_RETRY_GAP)
                    .unwrap_or(false);
                let app2 = app.clone();
                let label2 = label.to_string();
                if recent {
                    log::warn(format!("El muro {n} vuelve a pedir inicio de sesión: el servidor rechaza la clave"));
                    if let Ok(mut d) = v.denied.lock() {
                        d.insert(label.to_string(), "rechazado".into());
                    }
                    std::thread::spawn(move || navigate_local(&app2, &label2, "sin-permiso.html"));
                } else {
                    log::info(format!("El muro {n} perdió la sesión de kiosco: se vuelve a abrir"));
                    std::thread::spawn(move || go(&app2, &label2));
                }
                return false;
            }
        }
    }
    true
}

fn strip_query(url: &Url) -> String {
    let mut u = url.clone();
    u.set_query(None);
    u.set_fragment(None);
    u.to_string()
}

fn on_page_load<R: Runtime>(app: &AppHandle<R>, w: &WebviewWindow<R>, p: &PageLoadPayload<'_>) {
    if p.event() != PageLoadEvent::Finished {
        return;
    }
    let v = viewer(app);
    let label = w.label().to_string();
    let url = p.url().clone();
    if let Ok(mut nav) = v.nav.write() {
        let local = servers::is_local_url(&url);
        nav.on_local.insert(label.clone(), local);
        if local {
            nav.expect_local.insert(label.clone(), false);
        }
    }
    if url.path() != kiosk::EXCHANGE_PAGE {
        return;
    }
    let error = url.query_pairs().find(|(k, _)| k == "error").map(|(_, v)| v.into_owned());
    if let Some(code) = error {
        log::warn(format!("El servidor no aceptó la clave de los muros en «{label}» (código {code})"));
        let motivo = match code.as_str() {
            "401" => "rechazado",
            "403" => "no_local",
            "429" => "espera",
            _ => "red",
        };
        if let Ok(mut d) = v.denied.lock() {
            d.insert(label.clone(), motivo.into());
        }
        let app2 = app.clone();
        std::thread::spawn(move || navigate_local(&app2, &label, "sin-permiso.html"));
        return;
    }
    let pending = v.pending_kiosk.lock().ok().and_then(|mut m| m.remove(&label));
    let Some(pending) = pending else { return };
    if origin_of(&url).as_deref() != Some(pending.origin.as_str())
        || pending.created.elapsed() > Duration::from_secs(60)
    {
        log::warn(format!("Intercambio de kiosco descartado en «{label}» (origen inesperado o caducado)"));
        return;
    }
    if let Err(e) = w.eval(kiosk::exchange_script(&pending.token, &pending.next)) {
        log::error(format!("No se pudo ejecutar el intercambio de kiosco en «{label}»: {e}"));
    }
}

/// Lleva la ventana a una página local del visor.
pub fn navigate_local<R: Runtime>(app: &AppHandle<R>, label: &str, page: &str) {
    let Some(w) = app.get_webview_window(label) else { return };
    let v = viewer(app);
    v.expect_local(label);
    let url = servers::local_page(page, &[("ventana", label)]);
    if let Err(e) = w.navigate(url) {
        log::error(format!("No se pudo abrir {page} en «{label}»: {e}"));
    }
}

fn navigate_remote<R: Runtime>(app: &AppHandle<R>, label: &str, url: Url) {
    if let Some(w) = app.get_webview_window(label) {
        if let Err(e) = w.navigate(url) {
            log::error(format!("No se pudo navegar en «{label}»: {e}"));
        }
    }
}

/// Resultado de intentar abrir el destino de una ventana (lo devuelve `reintentar` a «Conectando…»).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct GoOutcome {
    pub estado: &'static str,
    pub titulo: String,
    pub mensaje: String,
    pub servidor: String,
    pub url: String,
}

impl GoOutcome {
    fn new(estado: &'static str, titulo: &str, mensaje: impl Into<String>, servidor: &str, url: &str) -> Self {
        Self { estado, titulo: titulo.into(), mensaje: mensaje.into(), servidor: servidor.into(), url: url.into() }
    }
}

/// Comprueba el servidor de la ventana y, si está disponible, la lleva a su destino (panel, muro con kiosco…).
pub fn go<R: Runtime>(app: &AppHandle<R>, label: &str) -> GoOutcome {
    let v = viewer(app);
    let Some(_turn) = GoTurn::take(&v, label) else {
        return GoOutcome::new("conectando", "Conectando…", "Comprobación en curso", "", "");
    };
    go_inner(app, &v, label)
}

/// Evita dos comprobaciones a la vez para la misma ventana (la página y el visor pueden pedirla juntas).
struct GoTurn {
    v: Arc<Viewer>,
    label: String,
}

impl GoTurn {
    fn take(v: &Arc<Viewer>, label: &str) -> Option<Self> {
        let mut g = v.going.lock().ok()?;
        g.insert(label.to_string()).then(|| Self { v: v.clone(), label: label.to_string() })
    }
}

impl Drop for GoTurn {
    fn drop(&mut self) {
        if let Ok(mut g) = self.v.going.lock() {
            g.remove(&self.label);
        }
    }
}

fn go_inner<R: Runtime>(app: &AppHandle<R>, v: &Arc<Viewer>, label: &str) -> GoOutcome {
    let Some(target) = v.target(label) else {
        return GoOutcome::new("error", "Ventana sin destino", "Cierra esta ventana y vuelve a abrirla.", "", "");
    };
    let server = match v.config().resolve(&target.server) {
        Ok(s) => s,
        Err(e) => return GoOutcome::new("error", "Servidor no configurado", e, &target.server, ""),
    };
    let origin = server.url.origin();
    if server.url.https {
        match pinning::probe(&server.url, PROBE_TIMEOUT) {
            Err(e) => {
                return GoOutcome::new(
                    "conectando",
                    "Conectando con el servidor…",
                    e.to_string(),
                    &server.name,
                    &origin,
                )
            }
            Ok(fp) => {
                if let Ok(mut o) = v.observed.lock() {
                    o.insert(origin.clone(), fp.clone());
                }
                match pinning::check(server.sha256.as_deref(), &fp) {
                    PinCheck::Match => {}
                    other => {
                        let (expected, first) = match &other {
                            PinCheck::Changed { expected, .. } => (Some(expected.clone()), false),
                            _ => (None, true),
                        };
                        log::warn(format!(
                            "Certificado de «{}» {}: {}",
                            server.name,
                            if first { "sin confirmar" } else { "CAMBIADO" },
                            fp
                        ));
                        if let Ok(mut c) = v.cert_views.lock() {
                            c.insert(
                                label.to_string(),
                                CertView { server: server.name.clone(), url: origin.clone(), expected, observed: fp },
                            );
                        }
                        navigate_local(app, label, "certificado.html");
                        let (t, m) = if first {
                            (
                                "Confirma el certificado del servidor",
                                "Es la primera vez que te conectas a este servidor.",
                            )
                        } else {
                            ("El certificado del servidor cambió", "No se abre hasta que lo revises.")
                        };
                        return GoOutcome::new("certificado", t, m, &server.name, &origin);
                    }
                }
            }
        }
    } else if let Err(e) = http::get(&server.url, "/api/health", None, PROBE_TIMEOUT) {
        return GoOutcome::new(
            "conectando",
            "Conectando con el servicio…",
            format!("El servicio de vídeo no responde todavía ({e}). Se reintenta cada 2 segundos."),
            &server.name,
            &origin,
        );
    }

    // Servidor disponible: destino de la ventana
    if let Some(n) = wall_of(label) {
        if !server.url.https && server.url.is_loopback() {
            match kiosk::read_token(&v.kiosk_token_path()) {
                Err(e) => {
                    log::warn(format!("Muro {n}: {e}"));
                    if let Ok(mut d) = v.denied.lock() {
                        d.insert(label.to_string(), e.code().into());
                    }
                    navigate_local(app, label, "sin-permiso.html");
                    return GoOutcome::new(
                        "sin_permiso",
                        "Sin permiso para abrir los muros",
                        e.to_string(),
                        &server.name,
                        &origin,
                    );
                }
                Ok(token) => {
                    if let Ok(mut m) = v.kiosk_attempts.lock() {
                        m.insert(label.to_string(), Instant::now());
                    }
                    if let Ok(mut m) = v.pending_kiosk.lock() {
                        m.insert(
                            label.to_string(),
                            PendingKiosk {
                                origin: origin.clone(),
                                next: target.path.clone(),
                                token,
                                created: Instant::now(),
                            },
                        );
                    }
                    match server.url.join(kiosk::EXCHANGE_PAGE) {
                        Ok(u) => navigate_remote(app, label, u),
                        Err(e) => return GoOutcome::new("error", "Dirección no válida", e, &server.name, &origin),
                    }
                    return GoOutcome::new("listo", "Abriendo el muro…", "", &server.name, &origin);
                }
            }
        }
    }
    match server.url.join(&target.path) {
        Ok(u) => {
            navigate_remote(app, label, u);
            GoOutcome::new("listo", "Abriendo…", "", &server.name, &origin)
        }
        Err(e) => GoOutcome::new("error", "Dirección no válida", e, &server.name, &origin),
    }
}

fn spawn_go<R: Runtime>(app: &AppHandle<R>, label: &str) {
    let app = app.clone();
    let label = label.to_string();
    std::thread::spawn(move || {
        let out = go(&app, &label);
        if out.estado == "conectando" {
            // «Conectando…» ya se muestra; la página reintenta cada 2 s
            navigate_local(&app, &label, "conectando.html");
        }
    });
}

pub fn open_panel<R: Runtime>(app: &AppHandle<R>, path: &str) {
    let v = viewer(app);
    let server = v.config().panel;
    let existed = app.get_webview_window(PANEL).is_some();
    let changed = v.target(PANEL).map(|t| t.path != path || t.server != server).unwrap_or(true);
    v.set_target(PANEL, &server, path);
    match ensure_window(app, PANEL) {
        Ok(w) => {
            let _ = w.show();
            let _ = w.unminimize();
            let _ = w.set_focus();
            // una ventana nueva arranca en «Conectando…», que ya llama a `reintentar`
            if existed && changed {
                spawn_go(app, PANEL);
            }
        }
        Err(e) => log::error(format!("No se pudo abrir el panel: {e}")),
    }
}

pub fn open_local_window<R: Runtime>(app: &AppHandle<R>, label: &str) {
    match ensure_window(app, label) {
        Ok(w) => {
            let _ = w.show();
            let _ = w.unminimize();
            let _ = w.set_focus();
        }
        Err(e) => log::error(format!("No se pudo abrir «{label}»: {e}")),
    }
}

// ============================================================================================ muros
fn current_monitors<R: Runtime>(app: &AppHandle<R>) -> Vec<MonitorInfo> {
    app.available_monitors()
        .map(|ms| {
            ms.iter()
                .map(|m| MonitorInfo {
                    name: m.name().cloned().unwrap_or_default(),
                    x: m.position().x,
                    y: m.position().y,
                    width: m.size().width,
                    height: m.size().height,
                    scale: m.scale_factor(),
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Muros que se abren con «Mostrar todos» o `--walls`: los configurados o uno por monitor (hasta 4).
pub fn default_walls(cfg: &ViewerConfig, monitors: usize) -> BTreeSet<u8> {
    if !cfg.walls.is_empty() {
        return cfg.walls.iter().map(|w| w.wall).collect();
    }
    (1..=monitors.clamp(1, 4) as u8).collect()
}

pub fn set_walls_open<R: Runtime>(app: &AppHandle<R>, walls: BTreeSet<u8>) {
    let v = viewer(app);
    if let Ok(mut open) = v.walls_open.lock() {
        *open = walls;
    }
    place_walls(app);
    refresh_wall_menu(app);
}

pub fn toggle_all_walls<R: Runtime>(app: &AppHandle<R>) {
    let v = viewer(app);
    let any_open = v.walls_open.lock().map(|o| !o.is_empty()).unwrap_or(false);
    if any_open {
        set_walls_open(app, BTreeSet::new());
    } else {
        let n = current_monitors(app).len();
        set_walls_open(app, default_walls(&v.config(), n));
    }
}

pub fn toggle_wall<R: Runtime>(app: &AppHandle<R>, n: u8) {
    let v = viewer(app);
    let mut open = v.walls_open.lock().map(|o| o.clone()).unwrap_or_default();
    if !open.remove(&n) {
        open.insert(n);
    }
    set_walls_open(app, open);
}

/// Coloca los muros abiertos en sus monitores (cada 5 s y tras cualquier cambio). Oculta los que se quedan sin
/// monitor y cierra los que ya no están abiertos.
pub fn place_walls<R: Runtime>(app: &AppHandle<R>) {
    let v = viewer(app);
    let open: Vec<u8> = v.walls_open.lock().map(|o| o.iter().copied().collect()).unwrap_or_default();
    let mons = current_monitors(app);
    let cfg = v.config();
    let plan = monitors::plan(&mons, &cfg.walls, &open);
    let new_keys: Vec<(u8, String)> = plan.iter().filter_map(|p| Some((p.wall, p.new_key.clone()?))).collect();
    if !new_keys.is_empty() {
        let _ = v.update_config(|c| {
            for (w, k) in &new_keys {
                c.wall_mut(*w).monitor_key = Some(k.clone());
            }
            Ok(())
        });
    }
    for p in &plan {
        let label = wall_label(p.wall);
        match &p.monitor {
            Some(m) => {
                let server = cfg.wall(p.wall).map(|w| w.server.clone()).unwrap_or_else(|| servers::LOCAL_SERVER.into());
                let fresh = app.get_webview_window(&label).is_none();
                let path = format!("/wall/{}", p.wall);
                let retarget = v.target(&label).map(|t| t.server != server || t.path != path).unwrap_or(true);
                v.set_target(&label, &server, &path);
                let w = match ensure_window(app, &label) {
                    Ok(w) => w,
                    Err(e) => {
                        log::error(format!("No se pudo crear el muro {}: {e}", p.wall));
                        continue;
                    }
                };
                let key = m.key();
                let moved = v.placed.lock().map(|pl| pl.get(&p.wall) != Some(&key)).unwrap_or(true);
                if moved || fresh {
                    let _ = w.set_fullscreen(false);
                    let _ = w.set_position(PhysicalPosition::new(m.x, m.y));
                    let _ = w.set_size(PhysicalSize::new(m.width, m.height));
                    let _ = w.set_fullscreen(true);
                    if let Ok(mut pl) = v.placed.lock() {
                        pl.insert(p.wall, key);
                    }
                    log::info(format!(
                        "Muro {} en el monitor {} ({}x{} a {}x)",
                        p.wall, m.name, m.width, m.height, m.scale
                    ));
                }
                if !w.is_visible().unwrap_or(false) {
                    let _ = w.show();
                }
                if !fresh && retarget {
                    spawn_go(app, &label);
                }
            }
            None => {
                if let Some(w) = app.get_webview_window(&label) {
                    if w.is_visible().unwrap_or(false) {
                        log::info(format!("Muro {}: su monitor no está conectado; se oculta", p.wall));
                        let _ = w.hide();
                    }
                }
                if let Ok(mut pl) = v.placed.lock() {
                    pl.remove(&p.wall);
                }
            }
        }
    }
    for n in 1..=4u8 {
        if !open.contains(&n) {
            if let Some(w) = app.get_webview_window(&wall_label(n)) {
                let _ = w.destroy();
                if let Ok(mut pl) = v.placed.lock() {
                    pl.remove(&n);
                }
            }
        }
    }
}

// ============================================================================================ bandeja
pub struct TrayItems<R: Runtime> {
    pub walls: Vec<CheckMenuItem<R>>,
    pub update_label: MenuItem<R>,
    pub autostart: CheckMenuItem<R>,
}

fn build_tray<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let v = viewer(app);
    let walls: Vec<CheckMenuItem<R>> = (1..=4u8)
        .map(|n| CheckMenuItem::with_id(app, format!("muro:{n}"), format!("Muro {n}"), true, false, None::<&str>))
        .collect::<tauri::Result<_>>()?;
    let mut wall_items: Vec<&dyn tauri::menu::IsMenuItem<R>> = Vec::new();
    let toggle = MenuItem::with_id(app, "muros:todos", "Mostrar u ocultar todos", true, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let reassign = MenuItem::with_id(app, "abrir:monitores", "Reasignar monitores…", true, None::<&str>)?;
    wall_items.push(&toggle);
    wall_items.push(&sep1);
    for w in &walls {
        wall_items.push(w);
    }
    wall_items.push(&sep2);
    wall_items.push(&reassign);
    let walls_menu = Submenu::with_items(app, "Muros", true, &wall_items)?;

    let update_label = MenuItem::with_id(
        app,
        "act:estado",
        v.update.lock().map(|u| u.label.clone()).unwrap_or_default(),
        true,
        None::<&str>,
    )?;
    let updates_menu = Submenu::with_items(
        app,
        "Actualizaciones",
        true,
        &[
            &update_label,
            &MenuItem::with_id(app, "act:buscar", "Buscar ahora", true, None::<&str>)?,
            &MenuItem::with_id(app, "act:volver", "Volver a la versión anterior…", true, None::<&str>)?,
        ],
    )?;
    let autostart = CheckMenuItem::with_id(
        app,
        "inicio",
        "Abrir al iniciar sesión",
        cfg!(windows),
        platform::autostart_get().is_some(),
        None::<&str>,
    )?;
    let menu = Menu::with_items(
        app,
        &[
            &MenuItem::with_id(app, "panel", "Abrir panel", true, None::<&str>)?,
            &walls_menu,
            &MenuItem::with_id(app, "estado", "Estado del sistema", true, None::<&str>)?,
            &updates_menu,
            &MenuItem::with_id(app, "abrir:servidores", "Servidores…", true, None::<&str>)?,
            &autostart,
            &MenuItem::with_id(app, "abrir:acerca", "Acerca de", true, None::<&str>)?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "salir", "Salir (la grabación sigue)", true, None::<&str>)?,
        ],
    )?;
    TrayIconBuilder::with_id(TRAY_ID)
        .icon(tauri::image::Image::new_owned(tray_icons::rgba(Level::Unknown), tray_icons::SIZE, tray_icons::SIZE))
        .tooltip(Level::Unknown.tooltip())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, ev| on_menu(app, ev))
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                off_ui(tray.app_handle(), |app| open_panel(app, "/"));
            }
        })
        .build(app)?;
    app.manage(TrayItems { walls, update_label, autostart });
    Ok(())
}

fn refresh_wall_menu<R: Runtime>(app: &AppHandle<R>) {
    let v = viewer(app);
    let open = v.walls_open.lock().map(|o| o.clone()).unwrap_or_default();
    if let Some(items) = app.try_state::<TrayItems<R>>() {
        for (i, item) in items.walls.iter().enumerate() {
            let _ = item.set_checked(open.contains(&(i as u8 + 1)));
        }
    }
}

/// Los menús, la bandeja y la segunda instancia llegan en el hilo de la interfaz: lo que crea ventanas o hace
/// E/S va a otro hilo (crear un WebView2 esperando en el hilo de la interfaz puede bloquearla en Windows).
fn off_ui<R: Runtime>(app: &AppHandle<R>, f: impl FnOnce(&AppHandle<R>) + Send + 'static) {
    let app = app.clone();
    std::thread::spawn(move || f(&app));
}

fn on_menu<R: Runtime>(app: &AppHandle<R>, ev: MenuEvent) {
    let id = ev.id().as_ref().to_string();
    if id == "salir" {
        log::info("Salir desde la bandeja (los servicios y la grabación siguen)");
        app.exit(0);
        return;
    }
    off_ui(app, move |app| on_menu_action(app, &id));
}

fn on_menu_action<R: Runtime>(app: &AppHandle<R>, id: &str) {
    match id {
        "panel" => open_panel(app, "/"),
        "estado" => open_panel(app, "/status"),
        "muros:todos" => toggle_all_walls(app),
        "act:estado" => {
            let restart = viewer(app).update.lock().ok().and_then(|u| u.restart_into.clone());
            match restart {
                Some(version) => restart_into(app, &version),
                None => open_local_window(app, "actualizaciones"),
            }
        }
        "act:buscar" | "act:volver" => open_local_window(app, "actualizaciones"),
        "inicio" => toggle_autostart(app),
        other => {
            if let Some(n) = other.strip_prefix("muro:").and_then(|n| n.parse().ok()) {
                toggle_wall(app, n);
            } else if let Some(name) = other.strip_prefix("abrir:") {
                open_local_window(app, name);
            }
        }
    }
}

fn toggle_autostart<R: Runtime>(app: &AppHandle<R>) {
    let enable = platform::autostart_get().is_none();
    let v = viewer(app);
    let exe = std::env::current_exe().unwrap_or_default();
    let cmd = platform::autostart_command(v.layout.as_ref().map(|l| l.vmshost()).as_deref(), &exe) + " --bandeja";
    let r = platform::autostart_set(enable.then_some(cmd.as_str()));
    if let Err(e) = &r {
        log::error(format!("Inicio con la sesión: {e}"));
    }
    if let Some(items) = app.try_state::<TrayItems<R>>() {
        let _ = items.autostart.set_checked(platform::autostart_get().is_some());
    }
}

// ============================================================================================ hilos
fn health_loop<R: Runtime>(app: AppHandle<R>) {
    let mut last = Level::Unknown;
    loop {
        let v = viewer(&app);
        let cfg = v.config();
        let h = match cfg.resolve(&cfg.panel) {
            Ok(s) => match http::get(&s.url, "/api/health", s.sha256.as_deref(), PROBE_TIMEOUT) {
                Ok(r) => health::parse(r.status, &r.body),
                Err(_) => health::unreachable(),
            },
            Err(_) => health::unreachable(),
        };
        if h.level != last {
            log::info(format!("Estado del servicio: {:?} → {:?}", last, h.level));
            last = h.level;
            if let Some(tray) = app.tray_by_id(TRAY_ID) {
                set_tray_level(&tray, h.level);
            }
        }
        if let Ok(mut cur) = v.health.lock() {
            *cur = h;
        }
        std::thread::sleep(HEALTH_EVERY);
    }
}

fn set_tray_level<R: Runtime>(tray: &TrayIcon<R>, level: Level) {
    let img = tauri::image::Image::new_owned(tray_icons::rgba(level), tray_icons::SIZE, tray_icons::SIZE);
    let _ = tray.set_icon(Some(img));
    let _ = tray.set_tooltip(Some(level.tooltip()));
}

fn monitors_loop<R: Runtime>(app: AppHandle<R>) {
    loop {
        std::thread::sleep(MONITORS_EVERY);
        let any = viewer(&app).walls_open.lock().map(|o| !o.is_empty()).unwrap_or(false);
        if any {
            place_walls(&app);
        }
    }
}

fn updates_loop<R: Runtime>(app: AppHandle<R>) {
    loop {
        check_updates(&app);
        std::thread::sleep(UPDATES_EVERY);
    }
}

pub fn read_public_status(v: &Viewer) -> Option<updates::PublicStatus> {
    std::fs::read(v.public_status_path()).ok().and_then(|b| updates::parse(&b))
}

fn check_updates<R: Runtime>(app: &AppHandle<R>) {
    let v = viewer(app);
    let view = updates::view(&v.version, read_public_status(&v).as_ref());
    let changed = v.update.lock().map(|u| *u != view).unwrap_or(false);
    if changed {
        if let Some(items) = app.try_state::<TrayItems<R>>() {
            let _ = items.update_label.set_text(&view.label);
        }
        if let Ok(mut u) = v.update.lock() {
            *u = view.clone();
        }
    }
    let Some(version) = view.restart_into else { return };
    if !viewer_binary_changed(&v, &version) {
        return;
    }
    let walls_open = v.walls_open.lock().map(|o| !o.is_empty()).unwrap_or(false);
    let panel = app.get_webview_window(PANEL);
    let panel_in_use =
        panel.as_ref().map(|w| w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false)).unwrap_or(false);
    let idle = platform::idle_time().map(|d| d >= PANEL_IDLE_BEFORE_RESTART).unwrap_or(!panel_in_use);
    if walls_open || !panel_in_use || idle {
        restart_into(app, &version);
    } else if let Some(w) = panel {
        let shown = v.panel_notice_shown.lock().ok().and_then(|s| s.clone());
        if shown.as_deref() != Some(version.as_str()) {
            let _ = w.eval(panel_notice_script(&version));
            if let Ok(mut s) = v.panel_notice_shown.lock() {
                *s = Some(version);
            }
        }
    }
}

/// ¿El `VMS.exe` de la versión instalada es distinto del que corre? Si solo cambió la aplicación web, el
/// visor es el mismo archivo (enlace duro) y no hace falta reiniciarlo.
fn viewer_binary_changed(v: &Viewer, version: &str) -> bool {
    if let Ok(cached) = v.restart_check.lock() {
        if let Some((ver, differs)) = cached.as_ref() {
            if ver == version {
                return *differs;
            }
        }
    }
    let Some(layout) = v.layout.as_ref() else { return false };
    let Some(next) = layout.viewer_exe(version).filter(|p| p.is_file()) else { return false };
    let differs = match (std::env::current_exe().ok().and_then(|p| std::fs::read(p).ok()), std::fs::read(&next).ok()) {
        (Some(a), Some(b)) => pinning::fingerprint_hex(&a) != pinning::fingerprint_hex(&b),
        _ => true,
    };
    if let Ok(mut c) = v.restart_check.lock() {
        *c = Some((version.to_string(), differs));
    }
    differs
}

fn panel_notice_script(version: &str) -> String {
    let text = format!(
        "Hay una versión nueva del visor ({version}). Se reiniciará cuando no lo estés usando (tras 60 segundos sin actividad)."
    );
    let text_js = serde_json::to_string(&text).unwrap_or_default();
    format!(
        r#"(() => {{
  if (document.getElementById("vms-visor-aviso")) return;
  const d = document.createElement("div");
  d.id = "vms-visor-aviso"; d.setAttribute("role", "status");
  d.style.cssText = "position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:2147483647;" +
    "background:#1e293b;color:#f8fafc;border:1px solid #38bdf8;border-radius:8px;padding:10px 16px;" +
    "font:14px system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.35);max-width:90vw";
  d.textContent = {text_js};
  document.body.appendChild(d);
}})();"#
    )
}

/// Reinicia el visor desde `versions\<version>\viewer\VMS.exe` y sale.
pub fn restart_into<R: Runtime>(app: &AppHandle<R>, version: &str) {
    let v = viewer(app);
    let Some(exe) = v.layout.as_ref().and_then(|l| l.viewer_exe(version)).filter(|p| p.is_file()) else {
        log::warn(format!("No se encuentra el visor de la versión {version}: no se reinicia"));
        return;
    };
    let mut args = vec!["--after-pid".to_string(), std::process::id().to_string()];
    if v.walls_open.lock().map(|o| !o.is_empty()).unwrap_or(false) {
        args.push("--walls".into());
    }
    let panel_visible = app.get_webview_window(PANEL).map(|w| w.is_visible().unwrap_or(false)).unwrap_or(false);
    if panel_visible {
        args.push("--panel".into());
    }
    if !args.iter().any(|a| a == "--walls" || a == "--panel") {
        args.push("--bandeja".into());
    }
    match std::process::Command::new(&exe).args(&args).spawn() {
        Ok(_) => {
            log::info(format!("Reiniciando el visor en la versión {version}"));
            app.exit(0);
        }
        Err(e) => log::error(format!("No se pudo reiniciar el visor ({}): {e}", exe.display())),
    }
}

// ============================================================================================ arranque
fn apply_options<R: Runtime>(app: &AppHandle<R>, o: &Options) {
    if o.walls {
        let v = viewer(app);
        let n = current_monitors(app).len();
        let walls = default_walls(&v.config(), n);
        set_walls_open(app, walls);
    }
    if o.panel {
        open_panel(app, "/");
    }
    if let Some(name) = &o.open {
        open_local_window(app, name);
    }
}

/// Los manejadores de los comandos IPC (también los usa la prueba de ACL con el runtime simulado).
pub fn handlers<R: Runtime>() -> impl Fn(tauri::ipc::Invoke<R>) -> bool + Send + Sync + 'static {
    use crate::commands::*;
    tauri::generate_handler![
        reintentar,
        diagnostico,
        servidores,
        probar_servidor,
        guardar_servidor,
        borrar_servidor,
        asignar_servidor_muro,
        certificado,
        confiar_certificado,
        monitores,
        asignar_monitor,
        acerca,
        actualizaciones,
        buscar_actualizaciones,
        volver_version_anterior,
        sin_permiso
    ]
}

/// Prepara el entorno del proceso antes de crear WebView2 (sin hilos todavía).
pub fn harden_environment() {
    if !cfg!(feature = "prueba") {
        // En la versión publicada nadie puede abrir el depurador ni cambiar el navegador por variables de entorno.
        for var in [
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
            "WEBVIEW2_USER_DATA_FOLDER",
            "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",
            "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER",
        ] {
            std::env::remove_var(var);
        }
    }
}

pub fn run() {
    harden_environment();
    let argv: Vec<String> = std::env::args().collect();
    let opts = parse_args(&argv);
    if let Some(pid) = opts.after_pid {
        // reinicio en otra versión: esperar a que la anterior suelte la instancia única
        platform::wait_pid_exit(pid, Duration::from_secs(15));
    }
    log::init(crate::paths::log_file());
    let version = env!("CARGO_PKG_VERSION");
    let (v, outcome) = Viewer::for_current_user(version);
    log::info(format!("Visor {version} arrancado ({opts:?}); configuración: {outcome:?}"));
    let v = Arc::new(v);
    let first = opts.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            let o = parse_args(&argv);
            log::info(format!("Segunda instancia: {o:?}"));
            off_ui(app, move |app| apply_options(app, &o));
        }))
        .manage(v)
        .invoke_handler(handlers())
        .setup(move |app| {
            let handle = app.handle().clone();
            build_tray(&handle)?;
            for f in [health_loop::<tauri::Wry>, monitors_loop::<tauri::Wry>, updates_loop::<tauri::Wry>] {
                let h = handle.clone();
                std::thread::spawn(move || f(h));
            }
            apply_options(&handle, &first);
            Ok(())
        })
        .on_window_event(|window, event| match event {
            WindowEvent::CloseRequested { api, .. } if window.label() == PANEL => {
                api.prevent_close();
                let _ = window.hide();
            }
            WindowEvent::Destroyed => {
                if let Some(n) = wall_of(window.label()) {
                    let app = window.app_handle();
                    let v = viewer(app);
                    let removed = v.walls_open.lock().map(|mut o| o.remove(&n)).unwrap_or(false);
                    if let Ok(mut pl) = v.placed.lock() {
                        pl.remove(&n);
                    }
                    if removed {
                        refresh_wall_menu(app);
                    }
                }
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("no se pudo crear el visor");
    app.run(|_app, event| {
        if let RunEvent::ExitRequested { api, code, .. } = event {
            // cerrar la última ventana no cierra el visor: sigue en la bandeja («Salir» sí lo cierra)
            if code.is_none() {
                api.prevent_exit();
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        std::iter::once("VMS.exe").chain(a.iter().copied()).map(String::from).collect()
    }

    #[test]
    fn command_line() {
        assert_eq!(parse_args(&args(&[])), Options { panel: true, ..Default::default() });
        assert_eq!(parse_args(&args(&["--walls"])), Options { walls: true, ..Default::default() });
        assert_eq!(
            parse_args(&args(&["--walls", "--panel"])),
            Options { walls: true, panel: true, ..Default::default() }
        );
        assert_eq!(parse_args(&args(&["--bandeja"])), Options { tray_only: true, ..Default::default() });
        let o = parse_args(&args(&["--abrir", "servidores", "--after-pid", "42"]));
        assert_eq!(o.open.as_deref(), Some("servidores"));
        assert_eq!(o.after_pid, Some(42));
        assert!(!o.panel);
        assert_eq!(parse_args(&args(&["--abrir", "../../x"])).open, None);
    }

    #[test]
    fn default_walls_follow_config_or_monitors() {
        let mut cfg = ViewerConfig::default();
        assert_eq!(default_walls(&cfg, 0), BTreeSet::from([1]));
        assert_eq!(default_walls(&cfg, 3), BTreeSet::from([1, 2, 3]));
        assert_eq!(default_walls(&cfg, 6), BTreeSet::from([1, 2, 3, 4]));
        cfg.wall_mut(2);
        cfg.wall_mut(4);
        assert_eq!(default_walls(&cfg, 1), BTreeSet::from([2, 4]));
    }

    #[test]
    fn release_builds_have_no_cdp() {
        if !cfg!(feature = "prueba") {
            std::env::set_var("VMS_VIEWER_CDP_PORT", "9222");
            assert_eq!(cdp_port(), None);
            assert!(!browser_args().contains("remote-debugging"));
        }
    }

    #[test]
    fn panel_notice_is_safe_text() {
        let js = panel_notice_script("2.1.0\"<b>");
        assert!(js.contains(r#"textContent = "Hay una versión nueva del visor (2.1.0\"<b>)"#));
    }
}
