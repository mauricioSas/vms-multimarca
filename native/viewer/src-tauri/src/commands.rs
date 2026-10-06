//! Comandos IPC de las páginas locales (`ui/`). Solo los pueden llamar las páginas locales del visor: la
//! capacidad `local` no tiene clave `remote` (PLAN-V2 §1.1). Actúan sobre la ventana que llama (`window`),
//! nunca sobre lo que diga la URL de la página.

use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Runtime, State, Window};

use crate::app::{self, GoOutcome};
use crate::config::ViewerConfig;
use crate::kiosk::{self, TokenError};
use crate::pinning::{self, display_fingerprint};
use crate::servers::{self, parse_server_url};
use crate::state::{wall_of, Viewer};
use crate::{github_update, http, log, platform, updates};

type Res<T> = Result<T, String>;

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Res<T> {
    tauri::async_runtime::spawn_blocking(f).await.map_err(|e| e.to_string())
}

// --------------------------------------------------------------------------------------------- conexión
#[tauri::command]
pub async fn reintentar<R: Runtime>(app: AppHandle<R>, window: Window<R>) -> Res<GoOutcome> {
    let label = window.label().to_string();
    blocking(move || app::go(&app, &label)).await
}

#[derive(Serialize)]
pub struct ServidorPanel {
    nombre: String,
    url: String,
    responde: bool,
    estado: String,
    version: Option<String>,
    detalle: String,
}

#[derive(Serialize)]
pub struct Diagnostico {
    version_visor: String,
    carpeta_datos: String,
    visor_json: String,
    instalacion: Option<String>,
    servidor: Option<ServidorPanel>,
    clave_muros: String,
    actualizaciones: Option<updates::PublicStatus>,
    estado_actualizaciones: String,
}

#[tauri::command]
pub async fn diagnostico(state: State<'_, Arc<Viewer>>) -> Res<Diagnostico> {
    let v = state.inner().clone();
    blocking(move || {
        let cfg = v.config();
        let servidor = cfg.resolve(&cfg.panel).ok().map(|s| {
            match http::get(&s.url, "/api/health", s.sha256.as_deref(), Duration::from_secs(3)) {
                Ok(r) => {
                    let h = crate::health::parse(r.status, &r.body);
                    let body: serde_json::Value = serde_json::from_slice(&r.body).unwrap_or_default();
                    let engine = body.get("engine").and_then(|e| e.get("running")).and_then(|b| b.as_bool());
                    ServidorPanel {
                        nombre: s.name.clone(),
                        url: s.url.origin(),
                        responde: true,
                        estado: format!("{:?}", h.level).to_lowercase(),
                        version: h.version,
                        detalle: match engine {
                            Some(true) => "Motor de vídeo en marcha".into(),
                            Some(false) => "El motor de vídeo NO está en marcha".into(),
                            None => String::new(),
                        },
                    }
                }
                Err(e) => ServidorPanel {
                    nombre: s.name.clone(),
                    url: s.url.origin(),
                    responde: false,
                    estado: "sin_conexion".into(),
                    version: None,
                    detalle: e.to_string(),
                },
            }
        });
        let clave_muros = match kiosk::read_token(&v.kiosk_token_path()) {
            Ok(_) => "legible".to_string(),
            Err(e) => e.code().to_string(),
        };
        let st = app::read_public_status(&v);
        Diagnostico {
            version_visor: v.version.clone(),
            carpeta_datos: v.data_dir.display().to_string(),
            visor_json: v.cfg_path.display().to_string(),
            instalacion: v.layout.as_ref().map(|l| format!("{} (versión {})", l.root.display(), l.version)),
            servidor,
            clave_muros,
            estado_actualizaciones: updates::view(&v.version, st.as_ref()).label,
            actualizaciones: st,
        }
    })
    .await
}

// --------------------------------------------------------------------------------------------- servidores
#[derive(Serialize)]
pub struct ServidorVista {
    nombre: String,
    url: String,
    remoto: bool,
    huella: Option<String>,
    muros: Vec<u8>,
    panel: bool,
    borrable: bool,
}

#[derive(Serialize)]
pub struct Servidores {
    servidores: Vec<ServidorVista>,
    muros: Vec<(u8, String)>,
    panel: String,
}

fn servidores_vista(cfg: &ViewerConfig) -> Servidores {
    let servidores = cfg
        .all_servers()
        .into_iter()
        .map(|s| ServidorVista {
            muros: cfg.walls.iter().filter(|w| w.server.eq_ignore_ascii_case(&s.name)).map(|w| w.wall).collect(),
            panel: cfg.panel.eq_ignore_ascii_case(&s.name),
            borrable: !s.name.eq_ignore_ascii_case(servers::LOCAL_SERVER),
            remoto: s.url.https,
            huella: s.sha256.as_deref().map(display_fingerprint),
            url: s.url.origin(),
            nombre: s.name,
        })
        .collect();
    let muros = (1..=4u8)
        .map(|n| (n, cfg.wall(n).map(|w| w.server.clone()).unwrap_or_else(|| servers::LOCAL_SERVER.into())))
        .collect();
    Servidores { servidores, muros, panel: cfg.panel.clone() }
}

#[tauri::command]
pub fn servidores(state: State<'_, Arc<Viewer>>) -> Servidores {
    servidores_vista(&state.config())
}

#[derive(Serialize)]
pub struct Prueba {
    url: String,
    remoto: bool,
    huella: Option<String>,
    responde: bool,
    mensaje: String,
}

#[tauri::command]
pub async fn probar_servidor(state: State<'_, Arc<Viewer>>, url: String) -> Res<Prueba> {
    let parsed = parse_server_url(&url)?;
    let v = state.inner().clone();
    blocking(move || {
        let origin = parsed.origin();
        if parsed.https {
            match pinning::probe(&parsed, Duration::from_secs(5)) {
                Ok(fp) => {
                    if let Ok(mut o) = v.observed.lock() {
                        o.insert(origin.clone(), fp.clone());
                    }
                    Prueba {
                        url: origin,
                        remoto: true,
                        huella: Some(display_fingerprint(&fp)),
                        responde: true,
                        mensaje: "Comprueba que esta huella es la que aparece en el servidor antes de guardarlo."
                            .into(),
                    }
                }
                Err(e) => Prueba { url: origin, remoto: true, huella: None, responde: false, mensaje: e.to_string() },
            }
        } else {
            match http::get(&parsed, "/api/health", None, Duration::from_secs(3)) {
                Ok(_) => Prueba {
                    url: origin,
                    remoto: false,
                    huella: None,
                    responde: true,
                    mensaje: "El servicio responde.".into(),
                },
                Err(e) => Prueba { url: origin, remoto: false, huella: None, responde: false, mensaje: e.to_string() },
            }
        }
    })
    .await
}

#[tauri::command]
pub fn guardar_servidor(
    state: State<'_, Arc<Viewer>>,
    nombre: String,
    url: String,
    huella: Option<String>,
) -> Res<Servidores> {
    let parsed = parse_server_url(&url)?;
    let pin = match (parsed.https, huella.as_deref()) {
        (false, _) => None,
        (true, None) => return Err("Pulsa «Probar» y confirma la huella antes de guardar.".into()),
        (true, Some(h)) => {
            let h = pinning::normalize_fingerprint(h).ok_or("La huella no es válida")?;
            let seen = state.observed.lock().ok().and_then(|o| o.get(&parsed.origin()).cloned());
            if seen.as_deref() != Some(h.as_str()) {
                return Err(
                    "La huella no coincide con la que acaba de presentar el servidor. Pulsa «Probar» otra vez.".into(),
                );
            }
            Some(h)
        }
    };
    state.update_config(|c| c.upsert_server(&nombre, &parsed.origin(), pin.as_deref()))?;
    log::info(format!("Servidor «{}» guardado: {}", nombre.trim(), parsed.origin()));
    Ok(servidores_vista(&state.config()))
}

#[tauri::command]
pub fn borrar_servidor(state: State<'_, Arc<Viewer>>, nombre: String) -> Res<Servidores> {
    state.update_config(|c| c.remove_server(&nombre))?;
    log::info(format!("Servidor «{nombre}» borrado"));
    Ok(servidores_vista(&state.config()))
}

/// `muro` 1-4, o 0 para el panel.
#[tauri::command]
pub fn asignar_servidor_muro<R: Runtime>(
    app: AppHandle<R>,
    state: State<'_, Arc<Viewer>>,
    muro: u8,
    servidor: String,
) -> Res<Servidores> {
    state.config().resolve(&servidor)?;
    if muro > 4 {
        return Err("Muro no válido (1 a 4)".into());
    }
    state.update_config(|c| {
        if muro == 0 {
            c.panel = servidor.clone();
        } else {
            c.wall_mut(muro).server = servidor.clone();
        }
        Ok(())
    })?;
    if muro > 0 {
        let a = app.clone();
        std::thread::spawn(move || app::place_walls(&a));
    }
    Ok(servidores_vista(&state.config()))
}

// --------------------------------------------------------------------------------------------- certificado
#[derive(Serialize)]
pub struct Certificado {
    servidor: String,
    url: String,
    esperada: Option<String>,
    observada: String,
    primera_vez: bool,
}

#[tauri::command]
pub fn certificado<R: Runtime>(window: Window<R>, state: State<'_, Arc<Viewer>>) -> Res<Certificado> {
    let views = state.cert_views.lock().map_err(|e| e.to_string())?;
    let c = views.get(window.label()).ok_or("No hay ningún certificado pendiente en esta ventana")?;
    Ok(Certificado {
        servidor: c.server.clone(),
        url: c.url.clone(),
        esperada: c.expected.as_deref().map(display_fingerprint),
        observada: display_fingerprint(&c.observed),
        primera_vez: c.expected.is_none(),
    })
}

#[tauri::command]
pub async fn confiar_certificado<R: Runtime>(
    app: AppHandle<R>,
    window: Window<R>,
    state: State<'_, Arc<Viewer>>,
    huella: String,
) -> Res<GoOutcome> {
    let label = window.label().to_string();
    let view = state
        .cert_views
        .lock()
        .ok()
        .and_then(|m| m.get(&label).cloned())
        .ok_or("No hay ningún certificado pendiente")?;
    let h = pinning::normalize_fingerprint(&huella).ok_or("La huella no es válida")?;
    if h != view.observed {
        return Err("La huella no es la que presentó el servidor".into());
    }
    state.update_config(|c| c.set_pin(&view.server, &h))?;
    log::warn(format!("Nueva huella de confianza para «{}»: {h}", view.server));
    if let Ok(mut m) = state.cert_views.lock() {
        m.remove(&label);
    }
    blocking(move || app::go(&app, &label)).await
}

// --------------------------------------------------------------------------------------------- monitores
#[derive(Serialize)]
pub struct MonitorVista {
    clave: String,
    nombre: String,
    x: i32,
    y: i32,
    ancho: u32,
    alto: u32,
    escala: f64,
}

#[derive(Serialize)]
pub struct MuroVista {
    muro: u8,
    clave: Option<String>,
    servidor: String,
    abierto: bool,
    conectado: bool,
}

#[derive(Serialize)]
pub struct Monitores {
    monitores: Vec<MonitorVista>,
    muros: Vec<MuroVista>,
}

#[tauri::command]
pub fn monitores<R: Runtime>(app: AppHandle<R>, state: State<'_, Arc<Viewer>>) -> Monitores {
    let mons: Vec<crate::monitors::MonitorInfo> = app
        .available_monitors()
        .map(|ms| {
            ms.iter()
                .map(|m| crate::monitors::MonitorInfo {
                    name: m.name().cloned().unwrap_or_default(),
                    x: m.position().x,
                    y: m.position().y,
                    width: m.size().width,
                    height: m.size().height,
                    scale: m.scale_factor(),
                })
                .collect()
        })
        .unwrap_or_default();
    let mons = crate::monitors::ordered(&mons);
    let cfg = state.config();
    let open = state.walls_open.lock().map(|o| o.clone()).unwrap_or_default();
    let keys: Vec<String> = mons.iter().map(|m| m.key()).collect();
    Monitores {
        muros: (1..=4u8)
            .map(|n| {
                let w = cfg.wall(n);
                let clave = w.and_then(|w| w.monitor_key.clone());
                MuroVista {
                    muro: n,
                    conectado: clave.as_ref().map(|k| keys.contains(k)).unwrap_or(false),
                    clave,
                    servidor: w.map(|w| w.server.clone()).unwrap_or_else(|| servers::LOCAL_SERVER.into()),
                    abierto: open.contains(&n),
                }
            })
            .collect(),
        monitores: mons
            .into_iter()
            .map(|m| MonitorVista {
                clave: m.key(),
                nombre: m.name,
                x: m.x,
                y: m.y,
                ancho: m.width,
                alto: m.height,
                escala: m.scale,
            })
            .collect(),
    }
}

#[tauri::command]
pub fn asignar_monitor<R: Runtime>(
    app: AppHandle<R>,
    state: State<'_, Arc<Viewer>>,
    muro: u8,
    clave: Option<String>,
) -> Res<()> {
    if !(1..=4).contains(&muro) {
        return Err("Muro no válido (1 a 4)".into());
    }
    let clave = clave.filter(|c| !c.trim().is_empty());
    state.update_config(|c| {
        // un monitor tiene un solo muro: si otro lo tenía, se le quita
        if let Some(k) = &clave {
            for w in c.walls.iter_mut().filter(|w| w.wall != muro && w.monitor_key.as_deref() == Some(k)) {
                w.monitor_key = None;
            }
        }
        c.wall_mut(muro).monitor_key = clave.clone();
        Ok(())
    })?;
    if let Ok(mut pl) = state.placed.lock() {
        pl.clear();
    }
    let a = app.clone();
    std::thread::spawn(move || app::place_walls(&a));
    Ok(())
}

// --------------------------------------------------------------------------------------------- acerca de
#[derive(Serialize)]
pub struct Acerca {
    producto: String,
    version: String,
    avisos: String,
    ruta_avisos: Option<String>,
}

#[tauri::command]
pub fn acerca(state: State<'_, Arc<Viewer>>) -> Acerca {
    let path = state.layout.as_ref().map(|l| l.notices());
    let avisos = path.as_ref().and_then(|p| std::fs::read_to_string(p).ok()).unwrap_or_else(|| {
        "Los avisos de terceros (licencias de los componentes incluidos) se instalan junto al programa en \
             THIRD_PARTY_NOTICES.txt. En esta copia de desarrollo no están disponibles."
            .into()
    });
    Acerca {
        producto: "VMS Multimarca".into(),
        version: state.version.clone(),
        avisos,
        ruta_avisos: path.map(|p| p.display().to_string()),
    }
}

// --------------------------------------------------------------------------------------------- actualizaciones
#[derive(Serialize)]
pub struct Actualizaciones {
    version_visor: String,
    etiqueta: String,
    estado: Option<updates::PublicStatus>,
    puede_actuar: bool,
}

#[tauri::command]
pub fn actualizaciones(state: State<'_, Arc<Viewer>>) -> Actualizaciones {
    let st = app::read_public_status(&state);
    Actualizaciones {
        version_visor: state.version.clone(),
        etiqueta: updates::view(&state.version, st.as_ref()).label,
        estado: st,
        puede_actuar: cfg!(windows) && state.layout.as_ref().map(|l| l.vmsctl().is_file()).unwrap_or(false),
    }
}

#[derive(Serialize)]
pub struct Resultado {
    ok: bool,
    mensaje: String,
}

fn run_vmsctl(v: &Viewer, args: String, ok_msg: &str) -> Resultado {
    let Some(vmsctl) = v.layout.as_ref().map(|l| l.vmsctl()).filter(|p| p.is_file()) else {
        return Resultado {
            ok: false,
            mensaje: "No se encuentra vmsctl.exe: esta copia del visor no está instalada.".into(),
        };
    };
    log::info(format!("Orden con elevación: vmsctl {args}"));
    match platform::run_elevated(&vmsctl, &args, Duration::from_secs(600)) {
        Ok(()) => Resultado { ok: true, mensaje: ok_msg.into() },
        Err(e) => {
            log::warn(format!("vmsctl {args}: {e:?}"));
            Resultado { ok: false, mensaje: e.message_es() }
        }
    }
}

#[tauri::command]
pub async fn buscar_actualizaciones(state: State<'_, Arc<Viewer>>) -> Res<Resultado> {
    let v = state.inner().clone();
    blocking(move || {
        run_vmsctl(&v, "update check --json".into(), "Comprobado. El estado se actualiza en unos segundos.")
    })
    .await
}

// --------------------------------------------------------------------------------------------- versión nueva (GitHub)
#[derive(Serialize)]
pub struct Novedades {
    instalada: Option<String>,
    novedad: Option<github_update::Novedad>,
    mensaje: String,
    puede_instalar: bool,
}

/// Versión del producto instalada (la carpeta `versions\<X>` de la que arranca el visor).
fn installed_version(v: &Viewer) -> Option<String> {
    v.layout.as_ref().map(|l| l.version.clone())
}

#[tauri::command]
pub async fn novedades(state: State<'_, Arc<Viewer>>) -> Res<Novedades> {
    let v = state.inner().clone();
    blocking(move || {
        let Some(installed) = installed_version(&v) else {
            return Novedades {
                instalada: None,
                novedad: None,
                mensaje: "Disponible solo en un equipo con el programa instalado (Windows).".into(),
                puede_instalar: false,
            };
        };
        match github_update::check(&installed) {
            Ok(n) => {
                if let Ok(mut slot) = v.novedad.lock() {
                    *slot = n.clone();
                }
                let mensaje = match &n {
                    Some(n) => format!("Hay una versión nueva: {}.", n.version),
                    None => format!("Tienes la última versión publicada ({installed})."),
                };
                Novedades { instalada: Some(installed), novedad: n, mensaje, puede_instalar: cfg!(windows) }
            }
            Err(e) => Novedades { instalada: Some(installed), novedad: None, mensaje: e, puede_instalar: false },
        }
    })
    .await
}

/// Descarga la versión nueva, comprueba su SHA-256 y la instala encima con elevación (UAC). La grabación solo se
/// para mientras se copian los archivos; la configuración y las grabaciones se conservan.
#[tauri::command]
pub async fn instalar_novedad(state: State<'_, Arc<Viewer>>) -> Res<Resultado> {
    let v = state.inner().clone();
    blocking(move || {
        let Some(installed) = installed_version(&v) else {
            return Resultado { ok: false, mensaje: "Esta copia del visor no está instalada.".into() };
        };
        let cached = v.novedad.lock().ok().and_then(|n| n.clone());
        let n = match cached {
            Some(n) => n,
            None => match github_update::check(&installed) {
                Ok(Some(n)) => n,
                Ok(None) => {
                    return Resultado { ok: true, mensaje: format!("Ya tienes la última versión ({installed}).") }
                }
                Err(e) => return Resultado { ok: false, mensaje: e },
            },
        };
        // Carpeta temporal de la persona (ProgramData no es escribible para ella; el instalador elevado sí la lee).
        let dir = std::env::temp_dir().join("VMSMultimarca-actualizacion");
        log::info(format!("Descargando la versión {} de {}", n.version, n.instalador_url));
        let exe = match github_update::download(&n, &dir) {
            Ok(p) => p,
            Err(e) => {
                log::warn(format!("Descarga de {}: {e}", n.version));
                return Resultado { ok: false, mensaje: e };
            }
        };
        let log_file = dir.join(format!("instalacion-{}.log", n.version));
        let args = github_update::installer_args(platform::installed_role().as_deref(), &log_file);
        log::info(format!("Instalando {} con elevación: {args}", exe.display()));
        match platform::run_elevated_shown(&exe, &args, Duration::from_secs(1800)) {
            Ok(()) => Resultado {
                ok: true,
                mensaje: format!("Versión {} instalada. El visor se reinicia solo en la versión nueva.", n.version),
            },
            Err(e) => {
                log::warn(format!("Instalador de {}: {e:?}", n.version));
                let detalle = match e {
                    platform::ElevateError::Failed(c) => {
                        format!("El instalador terminó con el código {c}. Detalle en {}.", log_file.display())
                    }
                    other => other.message_es(),
                };
                Resultado { ok: false, mensaje: detalle }
            }
        }
    })
    .await
}

/// Motivo para el registro del actualizador: una línea, sin comillas ni caracteres de control.
pub fn clean_reason(motivo: &str) -> String {
    let t: String = motivo
        .chars()
        .filter(|c| !c.is_control() && !matches!(c, '"' | '\\' | '%' | '^' | '&' | '|' | '<' | '>'))
        .take(200)
        .collect();
    let t = t.trim();
    if t.is_empty() {
        "Vuelta atrás pedida desde la bandeja del visor".into()
    } else {
        t.to_string()
    }
}

#[tauri::command]
pub async fn volver_version_anterior(state: State<'_, Arc<Viewer>>, motivo: String) -> Res<Resultado> {
    let v = state.inner().clone();
    let args = format!("update rollback --json --reason \"{}\"", clean_reason(&motivo));
    blocking(move || {
        run_vmsctl(&v, args, "Vuelta atrás en marcha. El visor se reiniciará solo en la versión anterior.")
    })
    .await
}

// --------------------------------------------------------------------------------------------- sin permiso
#[derive(Serialize)]
pub struct SinPermiso {
    codigo: String,
    titulo: String,
    mensaje: String,
    archivo: String,
    grupo: String,
    muro: Option<u8>,
}

pub fn sin_permiso_texto(codigo: &str) -> (&'static str, &'static str) {
    match codigo {
        "sin_permiso" => (
            "Sin permiso para abrir los muros",
            "Tu usuario de Windows no está en el grupo «VMS Operadores». Pide a un administrador que te añada \
             (o que abra los muros con la cuenta de muros) y vuelve a iniciar sesión en Windows.",
        ),
        "sin_token" => (
            "Este equipo no tiene la clave de los muros",
            "Falta el archivo de la clave. Un administrador puede crearla con «vmsctl kiosk rotate» o reinstalando el programa.",
        ),
        "token_invalido" | "error_lectura" => (
            "No se pudo leer la clave de los muros",
            "El archivo de la clave está dañado. Un administrador puede regenerarla con «vmsctl kiosk rotate».",
        ),
        "rechazado" => (
            "El servicio no acepta la clave de los muros",
            "La clave cambió o el modo kiosco está desactivado. Un administrador puede rotarla con «vmsctl kiosk rotate». \
             Se reintenta cada 30 segundos.",
        ),
        "no_local" => (
            "Los muros solo se abren en el propio equipo",
            "La entrada sin contraseña de los muros solo funciona contra el servicio de este mismo PC.",
        ),
        "espera" => ("Demasiados intentos", "Se reintenta en unos segundos."),
        _ => ("No se pudo abrir el muro", "Se reintenta automáticamente."),
    }
}

#[tauri::command]
pub fn sin_permiso<R: Runtime>(window: Window<R>, state: State<'_, Arc<Viewer>>) -> SinPermiso {
    let codigo = state
        .denied
        .lock()
        .ok()
        .and_then(|d| d.get(window.label()).cloned())
        .unwrap_or_else(|| TokenError::PermissionDenied.code().into());
    let (titulo, mensaje) = sin_permiso_texto(&codigo);
    SinPermiso {
        codigo,
        titulo: titulo.into(),
        mensaje: mensaje.into(),
        archivo: state.kiosk_token_path().display().to_string(),
        grupo: "VMS Operadores".into(),
        muro: wall_of(window.label()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reasons_cannot_break_the_command_line() {
        assert_eq!(clean_reason("  "), "Vuelta atrás pedida desde la bandeja del visor");
        assert_eq!(clean_reason("fallo \"x\" & del C:\\ | y\n"), "fallo x  del C:  y");
        assert_eq!(clean_reason(&"a".repeat(500)).len(), 200);
    }

    #[test]
    fn every_denied_code_has_spanish_text() {
        for c in
            ["sin_permiso", "sin_token", "token_invalido", "error_lectura", "rechazado", "no_local", "espera", "red"]
        {
            let (t, m) = sin_permiso_texto(c);
            assert!(!t.is_empty() && !m.is_empty());
        }
        assert_eq!(sin_permiso_texto("sin_permiso").0, "Sin permiso para abrir los muros");
    }
}
