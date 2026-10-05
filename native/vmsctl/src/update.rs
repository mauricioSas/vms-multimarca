//! `vmsctl update check|status|rollback|lock|unlock`: habla con `VMSUpdater` por su tubería de control
//! `\\.\pipe\VMSMultimarca.updater` (CONTRATO §15.2). Una línea JSON de petición y una de respuesta.
//! Si el actualizador responde `{"ok": false, "error": "busy"}`, el error de `vmsctl` lleva `error.code = "busy"`
//! (lo mira el instalador: «hay una actualización en curso»).
//!
//! La tubería solo admite SYSTEM y Administradores con el token elevado: desde una consola normal (o la
//! bandeja sin UAC) la respuesta es «acceso denegado» → código 11. En desarrollo (macOS/Linux) la tubería
//! es un socket Unix (`VMS_UPDATER_PIPE`, por defecto `<datos>/updater/control.sock`).
//!
//! Suplantación (revisión v2, Seguridad ALTO 2): se abre con `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION`
//! (un servidor falso que se quede con el nombre solo podría *identificar* al administrador, nunca actuar como
//! él) y, antes de escribir nada, se comprueba que el proceso servidor es de SYSTEM (`VMSUpdater`).

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

/// ¿Es SYSTEM la cuenta del proceso que atiende la tubería `pipe`?
#[cfg(windows)]
fn server_is_system(pipe: &std::fs::File) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::Security::{
        GetTokenInformation, IsWellKnownSid, TokenUser, WinLocalSystemSid, TOKEN_QUERY, TOKEN_USER,
    };
    use windows_sys::Win32::System::Pipes::GetNamedPipeServerProcessId;
    use windows_sys::Win32::System::Threading::{OpenProcess, OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION};

    struct Owned(HANDLE);
    impl Drop for Owned {
        fn drop(&mut self) {
            // SAFETY: handle propio, abierto aquí y cerrado una sola vez.
            unsafe { CloseHandle(self.0) };
        }
    }
    let mut pid: u32 = 0;
    // SAFETY: handle válido de la tubería abierta; `pid` es un u32 de esta función.
    if unsafe { GetNamedPipeServerProcessId(pipe.as_raw_handle() as HANDLE, &mut pid) } == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: llamada sin punteros; el handle devuelto se cierra con `Owned`.
    let proc_h = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if proc_h.is_null() {
        return Err(io::Error::last_os_error());
    }
    let proc_h = Owned(proc_h);
    let mut tok: HANDLE = std::ptr::null_mut();
    // SAFETY: `tok` recibe un handle nuevo que se cierra con `Owned`.
    if unsafe { OpenProcessToken(proc_h.0, TOKEN_QUERY, &mut tok) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let tok = Owned(tok);
    let mut buf = vec![0u8; 256];
    let mut len: u32 = 0;
    // SAFETY: `buf` tiene `buf.len()` bytes; TokenUser cabe de sobra en 256 (TOKEN_USER + un SID).
    if unsafe { GetTokenInformation(tok.0, TokenUser, buf.as_mut_ptr().cast(), buf.len() as u32, &mut len) } == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: GetTokenInformation(TokenUser) deja un TOKEN_USER al principio de `buf`.
    let user = unsafe { std::ptr::read_unaligned(buf.as_ptr().cast::<TOKEN_USER>()) };
    // SAFETY: el SID apunta dentro de `buf`, que sigue vivo.
    Ok(unsafe { IsWellKnownSid(user.User.Sid, WinLocalSystemSid) } != 0)
}

#[cfg(windows)]
fn connect_and_send(path: &std::path::Path, msg: &Value) -> io::Result<Value> {
    use std::os::windows::fs::OpenOptionsExt;
    const ERROR_PIPE_BUSY: i32 = 231;
    /// `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION`: el servidor solo puede identificar al cliente.
    const SQOS_IDENTIFICATION: u32 = 0x0010_0000 | 0x0001_0000;
    let mut last = None;
    for _ in 0..25 {
        match std::fs::OpenOptions::new().read(true).write(true).security_qos_flags(SQOS_IDENTIFICATION).open(path) {
            Ok(f) => {
                if !server_is_system(&f)? {
                    return Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        "la tubería del actualizador no la atiende SYSTEM (posible suplantación): no se envía nada",
                    ));
                }
                return exchange(f, msg);
            }
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

/// Código de error del actualizador que se conserva tal cual en `error.code` (el resto: `windows_error`).
fn updater_error_code(resp: &Value) -> &'static str {
    match resp.get("error").and_then(Value::as_str) {
        Some("busy") => "busy",
        Some("bad_request") => "bad_request",
        Some("rollback_failed") => "rollback_failed",
        Some("unknown_command") => "unknown_command",
        Some("internal") => "internal",
        _ => "windows_error",
    }
}

fn answer(resp: Value, text: impl Fn(&Value) -> String) -> Result<Outcome, CtlError> {
    if resp.get("ok").and_then(Value::as_bool) == Some(false) {
        let code = updater_error_code(&resp);
        let why = match resp.get("message_es").and_then(Value::as_str) {
            Some(m) if !m.is_empty() => m.to_string(),
            _ => resp.get("error").map(|e| e.to_string()).unwrap_or_else(|| "sin detalle".into()),
        };
        let mut err = CtlError::windows(format!("el actualizador respondió con un error: {why}")).with_data(resp);
        err.code = code;
        return Err(err);
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

/// Cerrojo del instalador (CONTRATO §15.2): mientras lo tenga `owner`, el actualizador no aplica nada.
pub fn lock(ctx: &Ctx, owner: &str, ttl: Option<&str>) -> Result<Outcome, CtlError> {
    let owner = check_owner(owner)?;
    let ttl_s = match ttl {
        None => 3600,
        Some(t) => t
            .parse::<u64>()
            .ok()
            .filter(|t| (1..=86_400).contains(t))
            .ok_or_else(|| CtlError::usage(format!("--ttl necesita segundos entre 1 y 86400, no «{t}»")))?,
    };
    let resp = request(ctx, &json!({"cmd": "lock", "owner": owner, "ttl_s": ttl_s}))?;
    answer(resp, |_| format!("Cerrojo del actualizador para «{owner}» ({ttl_s} s)."))
}

pub fn unlock(ctx: &Ctx, owner: &str) -> Result<Outcome, CtlError> {
    let owner = check_owner(owner)?;
    let resp = request(ctx, &json!({"cmd": "unlock", "owner": owner}))?;
    answer(resp, |_| format!("Cerrojo del actualizador de «{owner}» suelto."))
}

fn check_owner(owner: &str) -> Result<&str, CtlError> {
    let owner = owner.trim();
    if owner.is_empty() || owner.len() > 64 || !owner.chars().all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c)) {
        return Err(CtlError::usage(format!("--owner no válido «{owner}» (letras, números, «-», «_» o «.»; máx. 64)")));
    }
    Ok(owner)
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

    /// Servidor de una sola petición: devuelve la petición recibida y responde `reply`.
    fn one_shot(ctx: &Ctx, reply: &'static str) -> std::thread::JoinHandle<Value> {
        std::fs::create_dir_all(ctx.data.updater_dir()).unwrap();
        let l = UnixListener::bind(ctx.data.updater_dir().join("control.sock")).unwrap();
        std::thread::spawn(move || {
            let (s, _) = l.accept().unwrap();
            let mut r = BufReader::new(s.try_clone().unwrap());
            let mut line = String::new();
            r.read_line(&mut line).unwrap();
            (&s).write_all(reply.as_bytes()).unwrap();
            serde_json::from_str(&line).unwrap()
        })
    }

    #[test]
    fn lock_and_unlock_speak_the_pipe_protocol_and_keep_busy() {
        // Hallazgo M1: «update lock|unlock» no existían y «busy» llegaba como «windows_error».
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        let h = one_shot(&ctx, "{\"ok\": true}\n");
        lock(&ctx, "installer", Some("3600")).unwrap();
        assert_eq!(h.join().unwrap(), json!({"cmd": "lock", "owner": "installer", "ttl_s": 3600}));
        std::fs::remove_file(ctx.data.updater_dir().join("control.sock")).unwrap();
        let h = one_shot(
            &ctx,
            "{\"ok\": false, \"error\": \"busy\", \"message_es\": \"Hay una actualización aplicándose\"}\n",
        );
        let e = lock(&ctx, "installer", None).unwrap_err();
        assert_eq!(h.join().unwrap()["ttl_s"], 3600);
        assert_eq!((e.exit, e.code), (vms_common::exit_codes::WINDOWS_ERROR, "busy"));
        assert!(e.message.contains("aplicándose"), "{}", e.message);
        std::fs::remove_file(ctx.data.updater_dir().join("control.sock")).unwrap();
        let h = one_shot(&ctx, "{\"ok\": true}\n");
        unlock(&ctx, "installer").unwrap();
        assert_eq!(h.join().unwrap(), json!({"cmd": "unlock", "owner": "installer"}));
        assert_eq!(lock(&ctx, "", None).unwrap_err().exit, vms_common::exit_codes::USAGE);
        assert_eq!(lock(&ctx, "x", Some("0")).unwrap_err().exit, vms_common::exit_codes::USAGE);
        assert_eq!(unlock(&ctx, "a b").unwrap_err().exit, vms_common::exit_codes::USAGE);
    }

    #[test]
    fn updater_not_running_is_a_clear_error() {
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        let e = status(&ctx).unwrap_err();
        assert!(e.message.contains("no está en marcha"), "{}", e.message);
    }
}
