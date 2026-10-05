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
//! | `<datos>` (solo archivos de primer nivel: `.env`) | servicios del puesto: leer |
//! | `config\`, `ops\`, `evidence\` | VMSBackend: modificar |
//! | `secrets\` (sin herencia) | VMSBackend: modificar; `internal.token`: VMSAnalytics, VMSHeartbeat y VMSCentral leen; `site.token`: VMSHeartbeat lee; `kiosk.token`: grupo «VMS Operadores» lee |
//! | `logs\` | servicios del puesto: modificar |
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
//! (`vmsctl kiosk rotate` ya lo hace).

use crate::cli::CtlError;
use crate::sys::{system32, Runner};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use vms_common::layout::{DataLayout, InstallLayout};
use vms_common::services::{Account, ServiceDef, ANALYTICS, BACKEND, CENTRAL, ENGINE, HEARTBEAT};
use vms_common::sid::{service_sid, well_known};

pub const OPERATORS_GROUP: &str = "VMS Operadores";

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

/// Pasos de `icacls`, en orden. `exists` dice si un archivo existe (los permisos por archivo solo se
/// aplican a los que ya están).
pub fn plan(
    data: &DataLayout,
    install: Option<&InstallLayout>,
    services: &[&ServiceDef],
    extra_recordings: Option<&Path>,
    exists: &dyn Fn(&Path) -> bool,
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

    let modify = |names: &[&str]| -> Vec<String> {
        names.iter().filter(|n| has(n)).map(|n| format!("{}:(OI)(CI)M", sid_of(n))).collect()
    };
    let read = |names: &[&str]| -> Vec<String> {
        names.iter().filter(|n| has(n)).map(|n| format!("{}:(OI)(CI)RX", sid_of(n))).collect()
    };

    reset_then(&mut step, data.config_dir(), modify(&[BACKEND]));
    reset_then(&mut step, data.ops_dir(), modify(&[BACKEND]));
    reset_then(&mut step, data.evidence_dir(), modify(&[BACKEND]));
    reset_then(&mut step, data.logs_dir(), modify(&virt));
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
    let secrets = data.secrets_dir();
    for (file, entries) in [
        ("internal.token", file_read(&[ANALYTICS, HEARTBEAT, CENTRAL])),
        ("site.token", file_read(&[HEARTBEAT])),
        ("kiosk.token", vec![format!("{OPERATORS_GROUP}:R")]),
    ] {
        let p = secrets.join(file);
        if exists(&p) && !entries.is_empty() {
            step(p.clone(), vec!["/reset".into()]);
            step(p, grant(entries));
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
        let steps = plan(&data, Some(&inst), &svcs, None, &|p| p.ends_with("internal.token"));
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
    }

    #[test]
    fn control_plan_has_no_analytics_or_heartbeat() {
        let data = DataLayout::new("/pd");
        let steps = plan(&data, None, &Role::Control.services(), Some(Path::new("/rec")), &|_| true);
        let lines = joined(&steps).join("\n");
        assert!(!lines.contains(&service_sid(ANALYTICS)) && !lines.contains(&service_sid(HEARTBEAT)));
        assert!(lines.contains("/rec /inheritance:r"));
        assert!(lines.contains("kiosk.token /grant:r VMS Operadores:R"));
    }

    #[test]
    fn apply_runs_icacls_and_stops_at_the_first_failure() {
        let data = DataLayout::new("/pd");
        let steps = plan(&data, None, &[by_name(BACKEND).unwrap()], None, &|_| false);
        let mut r = FakeRunner::default();
        assert_eq!(apply(&mut r, &steps).unwrap(), steps.len());
        assert!(r.calls.iter().all(|c| c.starts_with("icacls.exe ") && c.ends_with(" /q")));
        let mut r = FakeRunner { codes: vec![("/pd/logs /grant".into(), 5)], ..Default::default() };
        let e = apply(&mut r, &steps).unwrap_err();
        assert_eq!(e.exit, vms_common::exit_codes::WINDOWS_ERROR);
        assert!(r.calls.last().unwrap().contains("/pd/logs /grant"));
    }
}
