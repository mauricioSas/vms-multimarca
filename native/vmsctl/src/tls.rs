//! `vmsctl tls setup --hostname <nombre> [--ip <IP>]… [--import-root]` y `vmsctl kiosk rotate`.
//!
//! - TLS: el certificado lo crea el propio backend (`python -m vms tls-cert`, con `cryptography`); aquí se
//!   apunta el `.env` a él (`VMS_TLS_CERT_FILE`/`VMS_TLS_KEY_FILE`) y, con `--import-root`, se importa en
//!   las raíces de confianza del equipo con `certutil -addstore Root` (solo el código de salida).
//! - Kiosco: token nuevo en `secrets\kiosk.token` con DPAPI de máquina y la ACL de CONTRATO §17.1
//!   (SYSTEM, Administradores, VMSBackend y el grupo «VMS Operadores»).

use crate::acl;
use crate::cli::{CtlError, Outcome};
use crate::ctx::Ctx;
use crate::envfile;
use crate::sys::{system32, Runner};
use serde_json::json;
use std::ffi::OsString;
use std::path::Path;
use vms_common::services::BACKEND;
use vms_common::sid::{service_sid, well_known};

pub fn setup(
    ctx: &Ctx,
    runner: &mut dyn Runner,
    python: &Path,
    hostname: &str,
    ips: &[String],
    import_root: bool,
) -> Result<Outcome, CtlError> {
    if hostname.trim().is_empty() || hostname.contains(char::is_whitespace) {
        return Err(CtlError::usage("indica el nombre del equipo en la red: --hostname <nombre>"));
    }
    let mut args: Vec<OsString> = ["-m", "vms", "tls-cert", "--host", hostname].iter().map(OsString::from).collect();
    for ip in ips {
        args.push("--ip".into());
        args.push(ip.into());
    }
    let env = vec![("VMS_DATA_DIR".to_string(), ctx.data.root.clone().into_os_string())];
    let r = runner.run(python, &args, &env).map_err(|e| CtlError::io(&e, &python.display().to_string()))?;
    if !r.ok() {
        return Err(CtlError::windows(format!(
            "no se pudo crear el certificado (código {}): {}",
            r.code, r.stderr_tail
        )));
    }
    let tls = ctx.data.secrets_dir().join("tls");
    let (cert, key) = (tls.join("vms.crt"), tls.join("vms.key"));
    let env_file = ctx.data.env_file();
    for (k, v) in [("VMS_TLS_CERT_FILE", &cert), ("VMS_TLS_KEY_FILE", &key)] {
        envfile::set(&env_file, k, &v.to_string_lossy())
            .map_err(|e| CtlError::io(&e, &env_file.display().to_string()))?;
    }
    let mut imported = false;
    if import_root {
        let r = runner
            .run(
                &system32("certutil.exe"),
                &[OsString::from("-f"), "-addstore".into(), "Root".into(), cert.clone().into()],
                &[],
            )
            .map_err(|e| CtlError::io(&e, "certutil"))?;
        if !r.ok() {
            return Err(CtlError::windows(format!("certutil no pudo importar el certificado (código {})", r.code)));
        }
        imported = true;
    }
    Ok(Outcome::new(
        json!({"cert": cert, "key": key, "env_file": env_file, "imported_root": imported}),
        format!(
            "Certificado listo para https://{hostname}:8643 (reinicia VMSBackend para usarlo). {}",
            if imported { "Importado como raíz de confianza en este equipo." } else { "" }
        ),
    ))
}

/// `backend_installed`: el SID de `NT SERVICE\VMSBackend` solo se puede usar en una ACL si el servicio existe.
pub fn kiosk_rotate(ctx: &Ctx, runner: &mut dyn Runner, backend_installed: bool) -> Result<Outcome, CtlError> {
    let bytes = vms_common::secret::random_bytes(32).map_err(|e| CtlError::io(&e, "generador aleatorio"))?;
    let token = vms_common::secret::b64url(&bytes);
    let path = ctx.data.secrets_dir().join("kiosk.token");
    vms_common::secret::write_secret(&path, token.as_bytes())
        .map_err(|e| CtlError::io(&e, &path.display().to_string()))?;
    let group = acl::ensure_operators_group()?;
    let mut args = vec![
        "/inheritance:r".to_string(),
        "/grant:r".to_string(),
        format!("*{}:F", well_known::LOCAL_SYSTEM),
        format!("*{}:F", well_known::ADMINISTRATORS),
        format!("{}:R", acl::OPERATORS_GROUP),
    ];
    if backend_installed {
        args.push(format!("*{}:R", service_sid(BACKEND)));
    }
    let steps = vec![acl::AclStep { path: path.clone(), args }];
    if cfg!(windows) {
        acl::apply(runner, &steps)?;
    }
    Ok(Outcome::new(
        json!({"file": path, "group_created": group, "protected": cfg!(windows)}),
        "Token de los muros cambiado: los muros abiertos tendrán que volver a entrar.",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sys::fake::FakeRunner;

    #[test]
    fn setup_runs_python_and_points_the_env_file() {
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        let mut r = FakeRunner::default();
        let out = setup(&ctx, &mut r, Path::new("/rt/python"), "caja-01", &["10.0.0.5".into()], true).unwrap();
        assert_eq!(r.calls[0], "python -m vms tls-cert --host caja-01 --ip 10.0.0.5");
        assert!(r.calls[1].starts_with("certutil.exe -f -addstore Root"));
        assert_eq!(out.data["imported_root"], true);
        let env = envfile::read(&ctx.data.env_file());
        assert!(env["VMS_TLS_CERT_FILE"].ends_with("vms.crt") && env["VMS_TLS_KEY_FILE"].ends_with("vms.key"));
        assert!(setup(&ctx, &mut r, Path::new("/rt/python"), "con espacio", &[], false).is_err());
    }

    #[test]
    fn kiosk_rotate_writes_a_new_token() {
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        let mut r = FakeRunner::default();
        kiosk_rotate(&ctx, &mut r, true).unwrap();
        let a = vms_common::secret::read_secret(&ctx.data.secrets_dir().join("kiosk.token")).unwrap();
        kiosk_rotate(&ctx, &mut r, true).unwrap();
        let b = vms_common::secret::read_secret(&ctx.data.secrets_dir().join("kiosk.token")).unwrap();
        assert_eq!(a.len(), 43);
        assert_ne!(a, b);
    }
}
