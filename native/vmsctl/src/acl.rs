//! `vmsctl acl apply`: permisos de la carpeta de datos por **SID** (PLAN-V2 §2.2, CONTRATO §13.2).
//!
//! Se aplican con `icacls.exe` y SID literales (`*S-1-5-80-…`, `*S-1-5-18`, `*S-1-5-32-544`): nunca nombres
//! localizados, y solo se mira el código de salida. Los SID de servicio se calculan (`vms_common::sid`), pero
//! Windows solo los acepta cuando el servicio ya existe (icacls: error 1332): se aplican después de crearlos.
//!
//! Resultado: nadie más que SYSTEM, Administradores y el servicio que lo necesita entra en la carpeta de
//! datos (los usuarios del equipo no ven grabaciones ni secretos; cierra el pendiente 1 de ESTADO.md).
//!
//! | Carpeta | Además de SYSTEM y Administradores |
//! |---|---|
//! | `<datos>` (solo archivos de primer nivel: `.env`) | servicios del puesto: leer. Cada archivo que ya exista vuelve a heredar (`/reset`): la v1 dejaba el `.env` sin herencia (solo SYSTEM y Administradores) y VMSCentral no podía leerlo |
//! | `config\`, `ops\`, `evidence\` | VMSBackend: modificar |
//! | `secrets\` (sin herencia) | VMSBackend: modificar; `internal.token`: VMSAnalytics, VMSHeartbeat y VMSCentral leen; `site.token`: VMSHeartbeat lee; `kiosk.token`: grupo «VMS Operadores» lee |
//! | `logs\` (la carpeta) | servicios del puesto: listar y **crear** archivos, sin modificar los de otros; CREATOR OWNER: modificar (cada servicio, solo lo que crea); VMSBackend: leer todo |
//! | `logs\<archivo>` que ya exista | el servicio dueño de ese nombre (`engine.log*` → VMSEngine, `vms.log*`/`audit.log*` → VMSBackend, `<Servicio>.log*`, `vmshost-<Servicio>.log*`, `status-<Servicio>.json`…): modificar |
//! | `recordings\` (y la carpeta de grabaciones externa) | VMSEngine y VMSBackend: modificar |
//! | `mediamtx\` | VMSEngine: leer; VMSBackend: modificar |
//! | `analytics\` | VMSAnalytics: modificar; VMSBackend: leer |
//! | `central\` | VMSCentral: modificar |
//! | `state\` | servicios: leer; `state\requests\`: servicios: modificar (peticiones de vuelta atrás) |
//! | `updater\` | Usuarios: leer (`public-status.json`) |
//! | `backups\` | — |
//! | instalación (`Program Files\VMSMultimarca`) | servicios: leer y ejecutar (sin quitar la herencia) |
//!
//! Ojo: un archivo que se reescribe con `atomic_write` hereda los permisos de su carpeta. Los permisos por
//! archivo (`internal.token`, `site.token`, `kiosk.token`) hay que volver a aplicarlos si se reescribe
//! (`vmsctl kiosk rotate` usa [`secret_file_steps`], la misma función que aquí).
//!
//! `logs\`: antes de aplicar la ACL se crean vacíos los registros principales de cada servicio
//! ([`precreate_logs`]): así ningún otro servicio puede «ocupar» ese nombre antes (p. ej. un `engine.log`
//! propio con líneas de 401 inventadas, que pausarían cámaras). `engine.log` además nunca se borra ni se
//! renombra (rota copiando y vaciando). Queda un riesgo residual en los registros de Python, que rotan
//! renombrando: en el instante entre renombrar y crear el nuevo, otro servicio comprometido podría crear
//! ese nombre (nunca `engine.log`).

use crate::cli::CtlError;
use crate::sys::{system32, Runner};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use vms_common::layout::{DataLayout, InstallLayout};
use vms_common::services::{Account, ServiceDef, ANALYTICS, BACKEND, CENTRAL, ENGINE, HEARTBEAT, SERVICES};
use vms_common::sid::{service_sid, well_known};

pub const OPERATORS_GROUP: &str = "VMS Operadores";
/// CREATOR OWNER.
const CREATOR_OWNER: &str = "S-1-3-0";
/// En la carpeta `logs\` (solo la carpeta): listar, leer atributos, recorrer y **crear archivos**.
const LOGS_DIR_RIGHTS: &str = "(RD,RA,REA,X,RC,S,WD)";

/// Registros de Python con nombre propio (además de `<Servicio>.log`, `vmshost-<Servicio>.log` y
/// `status-<Servicio>.json`, que son de cada servicio).
const PY_LOGS: &[(&str, &str)] = &[
    (ENGINE, "engine.log"),
    (BACKEND, "vms.log"),
    (BACKEND, "audit.log"),
    (BACKEND, "engine-run.log"),
    (ANALYTICS, "analytics.log"),
    (HEARTBEAT, "heartbeat.log"),
];

fn is_base_or_rotated(name: &str, base: &str) -> bool {
    name.eq_ignore_ascii_case(base)
        || (name.len() > base.len() + 1
            && name[..base.len()].eq_ignore_ascii_case(base)
            && name.as_bytes()[base.len()] == b'.'
            && name[base.len() + 1..].bytes().all(|b| b.is_ascii_digit()))
}

/// Servicio dueño de un archivo de `logs\` (por su nombre), si es de alguno.
pub fn log_owner(name: &str) -> Option<&'static str> {
    for def in SERVICES.iter().filter(|d| d.account == Account::Virtual) {
        let s = def.name;
        if is_base_or_rotated(name, &def.log_name())
            || is_base_or_rotated(name, &format!("vmshost-{s}.log"))
            || name.eq_ignore_ascii_case(&format!("status-{s}.json"))
        {
            return Some(s);
        }
    }
    PY_LOGS.iter().find(|(_, base)| is_base_or_rotated(name, base)).map(|(s, _)| *s)
}

/// Registros principales de un servicio (los que [`precreate_logs`] deja creados).
fn primary_logs(def: &ServiceDef) -> Vec<String> {
    let mut v = vec![def.log_name(), format!("vmshost-{}.log", def.name)];
    v.extend(PY_LOGS.iter().filter(|(s, _)| *s == def.name).map(|(_, b)| b.to_string()));
    v
}

/// Crea vacíos los registros principales de los servicios del puesto que aún no existan. Devuelve cuántos.
pub fn precreate_logs(data: &DataLayout, services: &[&ServiceDef]) -> std::io::Result<usize> {
    let dir = data.logs_dir();
    std::fs::create_dir_all(&dir)?;
    let mut n = 0;
    for def in services.iter().filter(|d| d.account == Account::Virtual) {
        for name in primary_logs(def) {
            let p = dir.join(&name);
            if !p.exists() {
                std::fs::OpenOptions::new().create(true).append(true).open(&p)?;
                n += 1;
            }
        }
    }
    Ok(n)
}

/// Archivos que ya existen y necesitan permisos propios.
#[derive(Clone, Debug, Default)]
pub struct Inventory {
    /// Archivos de primer nivel de la carpeta de datos (`.env`…).
    pub root_files: Vec<String>,
    /// Archivos de `logs\`.
    pub log_files: Vec<String>,
    /// Archivos de `secrets\`.
    pub secret_files: Vec<String>,
}

fn file_names(dir: &Path) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(dir)
        .map(|rd| {
            rd.filter_map(Result::ok)
                .filter(|e| e.file_type().is_ok_and(|t| t.is_file()))
                .filter_map(|e| e.file_name().to_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    v.sort();
    v
}

impl Inventory {
    pub fn scan(data: &DataLayout) -> Self {
        Self {
            root_files: file_names(&data.root),
            log_files: file_names(&data.logs_dir()),
            secret_files: file_names(&data.secrets_dir()),
        }
    }
}

/// Permisos de un archivo de `secrets\`: vuelve a heredar de la carpeta (SYSTEM, Administradores y
/// VMSBackend) y además lo leen `readers`. La usan `acl apply` y `kiosk rotate`.
pub fn secret_file_steps(data: &DataLayout, file: &str, readers: Vec<String>) -> Vec<AclStep> {
    let p = data.secrets_dir().join(file);
    let mut steps = vec![AclStep { path: p.clone(), args: vec!["/reset".into()] }];
    if !readers.is_empty() {
        let mut args = vec!["/grant:r".to_string()];
        args.extend(readers);
        steps.push(AclStep { path: p, args });
    }
    steps
}

/// Lectores de `kiosk.token`.
pub fn kiosk_token_readers() -> Vec<String> {
    vec![format!("{OPERATORS_GROUP}:R")]
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AclStep {
    pub path: PathBuf,
    pub args: Vec<String>,
}

fn sid_of(name: &str) -> String {
    format!("*{}", service_sid(name))
}

fn base_grants() -> Vec<String> {
    vec![format!("*{}:(OI)(CI)F", well_known::LOCAL_SYSTEM), format!("*{}:(OI)(CI)F", well_known::ADMINISTRATORS)]
}

/// Pasos de `icacls`, en orden. `inv`: los archivos que ya existen (los permisos por archivo solo se
/// aplican a los que están).
pub fn plan(
    data: &DataLayout,
    install: Option<&InstallLayout>,
    services: &[&ServiceDef],
    extra_recordings: Option<&Path>,
    inv: &Inventory,
) -> Vec<AclStep> {
    let virt: Vec<&str> = services.iter().filter(|s| s.account == Account::Virtual).map(|s| s.name).collect();
    let has = |n: &str| virt.contains(&n);
    let mut steps = Vec::new();
    let mut step = |path: PathBuf, args: Vec<String>| steps.push(AclStep { path, args });
    let grant = |entries: Vec<String>| -> Vec<String> {
        let mut a = vec!["/grant:r".to_string()];
        a.extend(entries);
        a
    };
    let reset_then = |steps: &mut dyn FnMut(PathBuf, Vec<String>), path: PathBuf, entries: Vec<String>| {
        steps(path.clone(), vec!["/reset".into()]);
        if !entries.is_empty() {
            steps(path, grant(entries));
        }
    };

    // Raíz: sin herencia de ProgramData (que deja leer a Usuarios), solo SYSTEM y Administradores.
    let mut root = vec!["/inheritance:r".to_string()];
    root.extend(grant(base_grants()));
    step(data.root.clone(), root);
    let root_files: Vec<String> = virt.iter().map(|n| format!("{}:(OI)(NP)R", sid_of(n))).collect();
    if !root_files.is_empty() {
        step(data.root.clone(), grant(root_files));
    }
    // Archivos de primer nivel (el .env): que hereden de la raíz aunque la v1 les quitara la herencia.
    for f in &inv.root_files {
        step(data.root.join(f), vec!["/reset".into()]);
    }

    let modify = |names: &[&str]| -> Vec<String> {
        names.iter().filter(|n| has(n)).map(|n| format!("{}:(OI)(CI)M", sid_of(n))).collect()
    };
    let read = |names: &[&str]| -> Vec<String> {
        names.iter().filter(|n| has(n)).map(|n| format!("{}:(OI)(CI)RX", sid_of(n))).collect()
    };

    reset_then(&mut step, data.config_dir(), modify(&[BACKEND]));
    reset_then(&mut step, data.ops_dir(), modify(&[BACKEND]));
    reset_then(&mut step, data.evidence_dir(), modify(&[BACKEND]));
    // logs\: cada servicio crea sus archivos y solo modifica los suyos (CREATOR OWNER y, para los que ya
    // existen, una entrada por archivo); el backend lee todos (sigue engine.log).
    let mut logs: Vec<String> = virt.iter().map(|n| format!("{}:{LOGS_DIR_RIGHTS}", sid_of(n))).collect();
    if !logs.is_empty() {
        logs.push(format!("*{CREATOR_OWNER}:(OI)(IO)M"));
    }
    reset_then(&mut step, data.logs_dir(), logs);
    if has(BACKEND) {
        step(data.logs_dir(), vec!["/grant".into(), format!("{}:(OI)(IO)R", sid_of(BACKEND))]);
    }
    for f in &inv.log_files {
        if let Some(owner) = log_owner(f).filter(|o| has(o)) {
            step(data.logs_dir().join(f), grant(vec![format!("{}:M", sid_of(owner))]));
        }
    }
    reset_then(&mut step, data.recordings_dir(), modify(&[ENGINE, BACKEND]));
    let mut mtx = read(&[ENGINE]);
    mtx.extend(modify(&[BACKEND]));
    reset_then(&mut step, data.mediamtx_dir(), mtx);
    let mut an = modify(&[ANALYTICS]);
    an.extend(read(&[BACKEND]));
    reset_then(&mut step, data.analytics_dir(), an);
    if has(CENTRAL) {
        reset_then(&mut step, data.central_dir(), modify(&[CENTRAL]));
    }
    reset_then(&mut step, data.state_dir(), read(&virt));
    reset_then(&mut step, data.state().requests_dir(), modify(&virt));
    reset_then(&mut step, data.updater_dir(), vec![format!("*{}:(OI)(CI)RX", well_known::USERS)]);
    reset_then(&mut step, data.backups_dir(), vec![]);

    // secrets\: sin herencia; solo el backend entra en la carpeta.
    let mut sec = vec!["/inheritance:r".to_string()];
    let mut entries = base_grants();
    entries.extend(modify(&[BACKEND]));
    sec.extend(grant(entries));
    step(data.secrets_dir(), sec);
    let file_read = |names: &[&str]| -> Vec<String> {
        names.iter().filter(|n| has(n)).map(|n| format!("{}:R", sid_of(n))).collect()
    };
    for (file, entries) in [
        ("internal.token", file_read(&[ANALYTICS, HEARTBEAT, CENTRAL])),
        ("site.token", file_read(&[HEARTBEAT])),
        ("kiosk.token", kiosk_token_readers()),
    ] {
        if inv.secret_files.iter().any(|f| f == file) && !entries.is_empty() {
            for s in secret_file_steps(data, file, entries) {
                step(s.path, s.args);
            }
        }
    }

    // Carpeta de grabaciones fuera de la carpeta de datos (p. ej. D:\Grabaciones CCTV): sin herencia del disco.
    if let Some(rec) = extra_recordings {
        let mut a = vec!["/inheritance:r".to_string()];
        let mut entries = base_grants();
        entries.extend(modify(&[ENGINE, BACKEND]));
        a.extend(grant(entries));
        step(rec.to_path_buf(), a);
    }

    // Instalación: los servicios leen y ejecutan su versión (se conserva la herencia de Program Files).
    if let Some(inst) = install {
        let rx = read(&virt);
        if !rx.is_empty() {
            step(inst.root.clone(), grant(rx));
        }
    }
    steps
}

pub fn apply(runner: &mut dyn Runner, steps: &[AclStep]) -> Result<usize, CtlError> {
    let icacls = system32("icacls.exe");
    for s in steps {
        let mut args: Vec<OsString> = vec![s.path.clone().into_os_string()];
        args.extend(s.args.iter().map(OsString::from));
        args.push("/q".into());
        let r = runner.run(&icacls, &args, &[]).map_err(|e| CtlError::io(&e, "icacls"))?;
        if !r.ok() {
            return Err(CtlError::windows(format!(
                "icacls falló ({}) en {} {}: {}",
                r.code,
                s.path.display(),
                s.args.join(" "),
                r.stderr_tail
            )));
        }
    }
    Ok(steps.len())
}

/// Crea el grupo local «VMS Operadores» si no existe (pueden abrir los muros: leen `kiosk.token`).
#[cfg(windows)]
pub fn ensure_operators_group() -> Result<bool, CtlError> {
    use windows_sys::Win32::NetworkManagement::NetManagement::{NetLocalGroupAdd, LOCALGROUP_INFO_1};
    const ERROR_ALIAS_EXISTS: u32 = 1379;
    const NERR_GROUP_EXISTS: u32 = 2223;
    let mut name: Vec<u16> = OPERATORS_GROUP.encode_utf16().chain(std::iter::once(0)).collect();
    let mut comment: Vec<u16> = "Pueden abrir los muros de vídeo de VMS Multimarca sin contraseña"
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    let info = LOCALGROUP_INFO_1 { lgrpi1_name: name.as_mut_ptr(), lgrpi1_comment: comment.as_mut_ptr() };
    let mut parm_err = 0u32;
    // SAFETY: estructura con cadenas UTF-16 válidas durante la llamada.
    let code = unsafe { NetLocalGroupAdd(std::ptr::null(), 1, &info as *const _ as *const u8, &mut parm_err) };
    match code {
        0 => Ok(true),
        ERROR_ALIAS_EXISTS | NERR_GROUP_EXISTS => Ok(false),
        other => Err(CtlError::windows(format!("no se pudo crear el grupo «{OPERATORS_GROUP}» (código {other})"))),
    }
}

#[cfg(not(windows))]
pub fn ensure_operators_group() -> Result<bool, CtlError> {
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sys::fake::FakeRunner;
    use vms_common::services::{by_name, Role};

    fn joined(steps: &[AclStep]) -> Vec<String> {
        // Rutas con «/» también en Windows (las pruebas comparan texto)
        steps.iter().map(|s| format!("{} {}", s.path.display(), s.args.join(" ")).replace('\\', "/")).collect()
    }

    #[test]
    fn store_plan_locks_the_root_and_grants_by_sid() {
        let data = DataLayout::new("/pd");
        let inst = InstallLayout::new("/pf");
        let svcs = Role::Store.services();
        let inv = Inventory {
            root_files: vec![".env".into()],
            log_files: vec!["engine.log".into(), "engine.log.1".into(), "vms.log".into(), "otro.txt".into()],
            secret_files: vec!["internal.token".into()],
        };
        let steps = plan(&data, Some(&inst), &svcs, None, &inv);
        let lines = joined(&steps);
        let backend = service_sid(BACKEND);
        let engine = service_sid(ENGINE);
        assert_eq!(lines[0], "/pd /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F");
        assert!(lines.iter().any(|l| l == &format!("/pd/config /grant:r *{backend}:(OI)(CI)M")));
        assert!(lines.iter().any(|l| l == &format!("/pd/mediamtx /grant:r *{engine}:(OI)(CI)RX *{backend}:(OI)(CI)M")));
        let sec = lines.iter().find(|l| l.starts_with("/pd/secrets /inheritance:r")).unwrap();
        assert!(sec.contains(&backend) && !sec.contains(&engine), "{sec}");
        let tok = lines.iter().find(|l| l.starts_with("/pd/secrets/internal.token /grant:r")).unwrap();
        assert!(tok.contains(&service_sid(ANALYTICS)) && tok.contains(&service_sid(HEARTBEAT)));
        assert!(!lines.iter().any(|l| l.contains("site.token")), "solo archivos que existen");
        // El actualizador es LocalSystem: no necesita entradas propias
        let upd = service_sid("VMSUpdater");
        assert!(!lines.iter().any(|l| l.contains(&upd)));
        // Nunca nombres localizados, salvo nuestro propio grupo
        assert!(!lines.iter().any(|l| l.contains("Administra") || l.contains("SYSTEM:")));
        assert!(lines.last().unwrap().starts_with("/pf /grant:r"));
        // .env de la v1 sin herencia: vuelve a heredar (la leen los servicios)
        assert!(lines.iter().any(|l| l == "/pd/.env /reset"));
        // logs\: crear sí, modificar lo ajeno no; engine.log solo lo modifica VMSEngine
        let analytics = service_sid(ANALYTICS);
        let logs = lines.iter().find(|l| l.starts_with("/pd/logs /grant:r")).unwrap();
        assert!(logs.contains(&format!("*{analytics}:(RD,RA,REA,X,RC,S,WD)")), "{logs}");
        assert!(logs.contains("*S-1-3-0:(OI)(IO)M") && !logs.contains(&format!("{analytics}:(OI)(CI)M")), "{logs}");
        assert!(lines.iter().any(|l| l == &format!("/pd/logs /grant *{backend}:(OI)(IO)R")));
        assert!(lines.iter().any(|l| l == &format!("/pd/logs/engine.log /grant:r *{engine}:M")));
        assert!(lines.iter().any(|l| l == &format!("/pd/logs/engine.log.1 /grant:r *{engine}:M")));
        assert!(lines.iter().any(|l| l == &format!("/pd/logs/vms.log /grant:r *{backend}:M")));
        assert!(!lines.iter().any(|l| l.contains("otro.txt")));
        assert!(!lines.iter().any(|l| l.starts_with("/pd/logs/engine.log") && l.contains(&analytics)));
    }

    #[test]
    fn log_files_belong_to_their_service_only() {
        assert_eq!(log_owner("engine.log"), Some(ENGINE));
        assert_eq!(log_owner("engine.log.9"), Some(ENGINE));
        assert_eq!(log_owner("engine-run.log"), Some(BACKEND));
        assert_eq!(log_owner("vms.log.3"), Some(BACKEND));
        assert_eq!(log_owner("audit.log.20"), Some(BACKEND));
        assert_eq!(log_owner("VMSAnalytics.log.1"), Some(ANALYTICS));
        assert_eq!(log_owner("vmshost-VMSHeartbeat.log"), Some(HEARTBEAT));
        assert_eq!(log_owner("status-VMSEngine.json"), Some(ENGINE));
        assert_eq!(log_owner("engine.log.rot-12"), None, "temporales de rotación: CREATOR OWNER");
        assert_eq!(log_owner("engine.logx"), None);
        assert_eq!(log_owner("vmshost-VMSUpdater.log"), None, "el actualizador es LocalSystem");
    }

    #[test]
    fn precreate_logs_claims_the_names_of_each_service() {
        let d = tempfile::tempdir().unwrap();
        let data = DataLayout::new(d.path());
        std::fs::create_dir_all(data.logs_dir()).unwrap();
        std::fs::write(data.logs_dir().join("vms.log"), b"de antes\n").unwrap();
        let n = precreate_logs(&data, &vms_common::services::Role::Store.services()).unwrap();
        for name in
            ["engine.log", "VMSBackend.log", "audit.log", "analytics.log", "heartbeat.log", "vmshost-VMSEngine.log"]
        {
            assert!(data.logs_dir().join(name).is_file(), "{name}");
        }
        assert_eq!(std::fs::read(data.logs_dir().join("vms.log")).unwrap(), b"de antes\n", "no pisa lo que hay");
        assert!(!data.logs_dir().join("vmshost-VMSUpdater.log").exists());
        assert!(n >= 10);
    }

    #[test]
    fn control_plan_has_no_analytics_or_heartbeat() {
        let data = DataLayout::new("/pd");
        let inv = Inventory { secret_files: vec!["kiosk.token".into(), "internal.token".into()], ..Default::default() };
        let steps = plan(&data, None, &Role::Control.services(), Some(Path::new("/rec")), &inv);
        let lines = joined(&steps).join("\n");
        assert!(!lines.contains(&service_sid(ANALYTICS)) && !lines.contains(&service_sid(HEARTBEAT)));
        assert!(lines.contains("/rec /inheritance:r"));
        assert!(lines.contains("kiosk.token /grant:r VMS Operadores:R"));
    }

    #[test]
    fn apply_runs_icacls_and_stops_at_the_first_failure() {
        let data = DataLayout::new("/pd");
        let steps = plan(&data, None, &[by_name(BACKEND).unwrap()], None, &Inventory::default());
        let mut r = FakeRunner::default();
        assert_eq!(apply(&mut r, &steps).unwrap(), steps.len());
        assert!(r.calls.iter().all(|c| c.starts_with("icacls.exe ") && c.ends_with(" /q")));
        let mut r = FakeRunner { codes: vec![("/pd/logs /grant".into(), 5)], ..Default::default() };
        let e = apply(&mut r, &steps).unwrap_err();
        assert_eq!(e.exit, vms_common::exit_codes::WINDOWS_ERROR);
        assert!(r.calls.last().unwrap().contains("/pd/logs /grant"));
    }
}
