//! `vmsctl update check|status|rollback`: habla con `VMSUpdater` por su tubería de control
//! `\\.\pipe\VMSMultimarca.updater` (CONTRATO §15.2). Una línea JSON de petición y una de respuesta.
//!
//! La tubería solo admite SYSTEM y Administradores con el token elevado: desde una consola normal (o la
//! bandeja sin UAC) la respuesta es «acceso denegado» → código 11. En desarrollo (macOS/Linux) la tubería
//! es un socket Unix (`VMS_UPDATER_PIPE`, por defecto `<datos>/updater/control.sock`).

use crate::cli::{CtlError, Outcome};
use crate::ctx::Ctx;
use serde_json::{json, Value};
use std::io::{self, BufRead, BufReader, Write};
use std::path::PathBuf;
use std::time::Duration;

pub const PIPE_NAME: &str = r"\\.\pipe\VMSMultimarca.updater";
const MAX_RESPONSE: u64 = 1024 * 1024;

fn pipe_path(ctx: &Ctx) -> PathBuf {
    if let Some(p) = std::env::var_os("VMS_UPDATER_PIPE").filter(|p| !p.is_empty()) {
        return PathBuf::from(p);
    }
    if cfg!(windows) {
        PathBuf::from(PIPE_NAME)
    } else {
        ctx.data.updater_dir().join("control.sock")
    }
}

fn exchange<S: io::Read + Write>(mut s: S, msg: &Value) -> io::Result<Value> {
    let mut line = msg.to_string();
    line.push('\n');
    s.write_all(line.as_bytes())?;
    s.flush()?;
    let mut reader = BufReader::new(io::Read::take(s, MAX_RESPONSE));
    let mut resp = String::new();
    reader.read_line(&mut resp)?;
    serde_json::from_str(resp.trim()).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

#[cfg(windows)]
fn connect_and_send(path: &std::path::Path, msg: &Value) -> io::Result<Value> {
    const ERROR_PIPE_BUSY: i32 = 231;
    let mut last = None;
    for _ in 0..25 {
        match std::fs::OpenOptions::new().read(true).write(true).open(path) {
            Ok(f) => return exchange(f, msg),
            Err(e) if e.raw_os_error() == Some(ERROR_PIPE_BUSY) => {
                last = Some(e);
                std::thread::sleep(Duration::from_millis(200));
            }
            Err(e) => return Err(e),
        }
    }
    Err(last.unwrap_or_else(|| io::Error::other("tubería ocupada")))
}

#[cfg(not(windows))]
fn connect_and_send(path: &std::path::Path, msg: &Value) -> io::Result<Value> {
    let s = std::os::unix::net::UnixStream::connect(path)?;
    s.set_read_timeout(Some(Duration::from_secs(120)))?;
    exchange(s, msg)
}

pub fn request(ctx: &Ctx, msg: &Value) -> Result<Value, CtlError> {
    let path = pipe_path(ctx);
    match connect_and_send(&path, msg) {
        Ok(v) => Ok(v),
        Err(e) if e.kind() == io::ErrorKind::NotFound || e.kind() == io::ErrorKind::ConnectionRefused => {
            Err(CtlError::windows("el servicio de actualizaciones (VMSUpdater) no está en marcha")
                .with_data(json!({"pipe": path})))
        }
        Err(e) => Err(CtlError::io(&e, "tubería del actualizador")),
    }
}

fn answer(resp: Value, text: impl Fn(&Value) -> String) -> Result<Outcome, CtlError> {
    if resp.get("ok").and_then(Value::as_bool) == Some(false) {
        let why = resp.get("error").map(|e| e.to_string()).unwrap_or_else(|| "sin detalle".into());
        return Err(CtlError::windows(format!("el actualizador respondió con un error: {why}")).with_data(resp));
    }
    let t = text(&resp);
    Ok(Outcome::new(resp, t))
}

pub fn check(ctx: &Ctx) -> Result<Outcome, CtlError> {
    let resp = request(ctx, &json!({"cmd": "check"}))?;
    answer(resp, |r| match r.get("found").and_then(Value::as_str) {
        Some(v) => format!("Hay una versión nueva: {v}. Se instalará en la ventana de mantenimiento."),
        None => "No hay versiones nuevas.".into(),
    })
}

pub fn status(ctx: &Ctx) -> Result<Outcome, CtlError> {
    let resp = request(ctx, &json!({"cmd": "status"}))?;
    answer(resp, |r| {
        format!(
            "Instalada: {} · estado: {} · último resultado: {}",
            r["installed"].as_str().unwrap_or("?"),
            r["state"].as_str().unwrap_or("?"),
            r["last_result"].as_str().unwrap_or("?")
        )
    })
}

pub fn rollback(ctx: &Ctx, to: Option<&str>, reason: Option<&str>) -> Result<Outcome, CtlError> {
    if let Some(v) = to {
        vms_common::state::validate_version(v)?;
    }
    let resp =
        request(ctx, &json!({"cmd": "rollback", "to": to, "reason": reason.unwrap_or("vmsctl update rollback")}))?;
    answer(resp, |r| format!("Vuelta atrás pedida ({}).", r["update_id"].as_str().unwrap_or("sin id")))
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    #[test]
    fn talks_one_json_line_each_way() {
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        std::fs::create_dir_all(ctx.data.updater_dir()).unwrap();
        let sock = ctx.data.updater_dir().join("control.sock");
        let l = UnixListener::bind(&sock).unwrap();
        let h = std::thread::spawn(move || {
            let (s, _) = l.accept().unwrap();
            let mut r = BufReader::new(s.try_clone().unwrap());
            let mut line = String::new();
            r.read_line(&mut line).unwrap();
            let req: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(req["cmd"], "rollback");
            assert_eq!(req["to"], "2.0.0");
            (&s).write_all(b"{\"ok\": true, \"update_id\": \"u-1\"}\n").unwrap();
        });
        let out = rollback(&ctx, Some("2.0.0"), Some("prueba")).unwrap();
        assert_eq!(out.data["update_id"], "u-1");
        h.join().unwrap();
        assert!(rollback(&ctx, Some("../x"), None).is_err());
    }

    #[test]
    fn updater_not_running_is_a_clear_error() {
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        let e = status(&ctx).unwrap_err();
        assert!(e.message.contains("no está en marcha"), "{}", e.message);
    }
}
