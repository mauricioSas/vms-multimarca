//! Lo que depende de Windows: DPAPI, elevación (UAC), inicio con la sesión (`HKCU\…\Run`), inactividad y
//! espera a que termine otro proceso. En macOS y Linux (solo desarrollo) son equivalentes mínimos.

use std::path::Path;
use std::time::Duration;

/// Resultado de lanzar una orden con elevación.
#[derive(Debug, PartialEq, Eq)]
pub enum ElevateError {
    /// La persona canceló el aviso de UAC (o no tiene credenciales de administrador).
    Cancelled,
    /// La orden terminó con un código distinto de 0 (CONTRATO §14.2).
    Failed(u32),
    TimedOut,
    Unsupported,
    Other(String),
}

impl ElevateError {
    pub fn message_es(&self) -> String {
        match self {
            Self::Cancelled => "Se canceló: hace falta confirmar con una cuenta de administrador.".into(),
            Self::Failed(11) => "Sin permisos para hacerlo (código 11).".into(),
            Self::Failed(12) => "La comprobación de salud falló (código 12).".into(),
            Self::Failed(20) => "Windows devolvió un error (código 20). Mira el registro del actualizador.".into(),
            Self::Failed(c) => format!("La orden terminó con el código {c}."),
            Self::TimedOut => "La orden sigue en marcha; mira el estado en «Estado del sistema».".into(),
            Self::Unsupported => "Solo está disponible en Windows.".into(),
            Self::Other(m) => format!("No se pudo lanzar: {m}"),
        }
    }
}

pub const AUTOSTART_VALUE: &str = "VMS Multimarca";

#[cfg(windows)]
mod imp {
    use super::*;
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use std::ptr::{null, null_mut};

    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, LocalFree, ERROR_CANCELLED, ERROR_FILE_NOT_FOUND, ERROR_SUCCESS, WAIT_OBJECT_0,
    };
    use windows_sys::Win32::Security::Cryptography::{
        CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };
    use windows_sys::Win32::System::Registry::{
        RegDeleteKeyValueW, RegGetValueW, RegSetKeyValueW, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, REG_SZ,
        RRF_RT_REG_SZ, RRF_SUBKEY_WOW6464KEY,
    };
    use windows_sys::Win32::System::SystemInformation::GetTickCount;
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE,
    };
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
    use windows_sys::Win32::UI::Shell::{
        ShellExecuteExW, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{SW_HIDE, SW_SHOWNORMAL};

    const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";

    fn wide(s: impl AsRef<OsStr>) -> Vec<u16> {
        s.as_ref().encode_wide().chain(std::iter::once(0)).collect()
    }

    pub fn dpapi_unprotect(blob: &[u8]) -> Result<Vec<u8>, String> {
        let input = CRYPT_INTEGER_BLOB { cbData: blob.len() as u32, pbData: blob.as_ptr() as *mut u8 };
        let mut output = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
        // SAFETY: punteros válidos durante la llamada; la salida la reserva Windows y se libera con LocalFree.
        let ok = unsafe {
            CryptUnprotectData(&input, null_mut(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut output)
        };
        if ok == 0 {
            return Err(format!("DPAPI no pudo descifrarlo (error {})", unsafe { GetLastError() }));
        }
        // SAFETY: Windows garantiza cbData bytes en pbData.
        let out = unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize) }.to_vec();
        unsafe {
            std::ptr::write_bytes(output.pbData, 0, output.cbData as usize);
            LocalFree(output.pbData as _);
        }
        Ok(out)
    }

    pub fn run_elevated(exe: &Path, args: &str, timeout: Duration) -> Result<(), ElevateError> {
        run_elevated_impl(exe, args, timeout, false)
    }

    /// Como `run_elevated`, pero con su ventana visible (el instalador enseña su barra de progreso).
    pub fn run_elevated_shown(exe: &Path, args: &str, timeout: Duration) -> Result<(), ElevateError> {
        run_elevated_impl(exe, args, timeout, true)
    }

    fn run_elevated_impl(exe: &Path, args: &str, timeout: Duration, show: bool) -> Result<(), ElevateError> {
        let verb = wide("runas");
        let file = wide(exe.as_os_str());
        let params = wide(args);
        let mut info: SHELLEXECUTEINFOW = unsafe { std::mem::zeroed() };
        info.cbSize = std::mem::size_of::<SHELLEXECUTEINFOW>() as u32;
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
        info.lpVerb = verb.as_ptr();
        info.lpFile = file.as_ptr();
        info.lpParameters = params.as_ptr();
        info.nShow = if show { SW_SHOWNORMAL } else { SW_HIDE };
        // SAFETY: estructura inicializada y cadenas vivas durante la llamada.
        if unsafe { ShellExecuteExW(&mut info) } == 0 {
            let err = unsafe { GetLastError() };
            return Err(if err == ERROR_CANCELLED {
                ElevateError::Cancelled
            } else {
                ElevateError::Other(format!("error {err}"))
            });
        }
        if info.hProcess.is_null() {
            return Ok(());
        }
        let ms = timeout.as_millis().min(u32::MAX as u128) as u32;
        // SAFETY: hProcess es un handle válido que cerramos aquí.
        unsafe {
            let waited = WaitForSingleObject(info.hProcess, ms);
            let mut code = 0u32;
            let got = GetExitCodeProcess(info.hProcess, &mut code);
            CloseHandle(info.hProcess);
            if waited != WAIT_OBJECT_0 {
                return Err(ElevateError::TimedOut);
            }
            if got == 0 {
                return Err(ElevateError::Other("sin código de salida".into()));
            }
            if code != 0 {
                return Err(ElevateError::Failed(code));
            }
        }
        Ok(())
    }

    /// Tipo de puesto instalado (`HKLM\SOFTWARE\VMSMultimarca\Role`, lo escribe el instalador).
    pub fn installed_role() -> Option<String> {
        let key = wide(r"SOFTWARE\VMSMultimarca");
        let name = wide("Role");
        let mut buf = vec![0u16; 64];
        let mut len = (buf.len() * 2) as u32;
        // SAFETY: búfer y longitud coherentes.
        let rc = unsafe {
            RegGetValueW(
                HKEY_LOCAL_MACHINE,
                key.as_ptr(),
                name.as_ptr(),
                RRF_RT_REG_SZ | RRF_SUBKEY_WOW6464KEY,
                null_mut(),
                buf.as_mut_ptr() as _,
                &mut len,
            )
        };
        if rc != ERROR_SUCCESS {
            return None;
        }
        let chars = (len as usize / 2).saturating_sub(1);
        Some(String::from_utf16_lossy(&buf[..chars.min(buf.len())]))
    }

    pub fn autostart_get() -> Option<String> {
        let key = wide(RUN_KEY);
        let name = wide(AUTOSTART_VALUE);
        let mut buf = vec![0u16; 2048];
        let mut len = (buf.len() * 2) as u32;
        // SAFETY: búfer y longitud coherentes.
        let rc = unsafe {
            RegGetValueW(
                HKEY_CURRENT_USER,
                key.as_ptr(),
                name.as_ptr(),
                RRF_RT_REG_SZ,
                null_mut(),
                buf.as_mut_ptr() as _,
                &mut len,
            )
        };
        if rc != ERROR_SUCCESS {
            return None;
        }
        let chars = (len as usize / 2).saturating_sub(1);
        Some(String::from_utf16_lossy(&buf[..chars.min(buf.len())]))
    }

    pub fn autostart_set(command: Option<&str>) -> Result<(), String> {
        let key = wide(RUN_KEY);
        let name = wide(AUTOSTART_VALUE);
        // SAFETY: cadenas terminadas en cero y longitudes en bytes.
        let rc = unsafe {
            match command {
                Some(cmd) => {
                    let data = wide(cmd);
                    RegSetKeyValueW(
                        HKEY_CURRENT_USER,
                        key.as_ptr(),
                        name.as_ptr(),
                        REG_SZ,
                        data.as_ptr() as _,
                        (data.len() * 2) as u32,
                    )
                }
                None => RegDeleteKeyValueW(HKEY_CURRENT_USER, key.as_ptr(), name.as_ptr()),
            }
        };
        if rc == ERROR_SUCCESS || (command.is_none() && rc == ERROR_FILE_NOT_FOUND) {
            Ok(())
        } else {
            Err(format!("no se pudo escribir en el registro (error {rc})"))
        }
    }

    pub fn idle_time() -> Option<Duration> {
        let mut lii = LASTINPUTINFO { cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32, dwTime: 0 };
        // SAFETY: estructura con cbSize correcto.
        if unsafe { GetLastInputInfo(&mut lii) } == 0 {
            return None;
        }
        let now = unsafe { GetTickCount() };
        Some(Duration::from_millis(now.wrapping_sub(lii.dwTime) as u64))
    }

    pub fn wait_pid_exit(pid: u32, timeout: Duration) -> bool {
        // SAFETY: handle propio que se cierra.
        unsafe {
            let h = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
            if h.is_null() {
                return true; // ya no existe
            }
            let r = WaitForSingleObject(h, timeout.as_millis().min(u32::MAX as u128) as u32);
            CloseHandle(h);
            r == WAIT_OBJECT_0
        }
    }
}

#[cfg(not(windows))]
mod imp {
    use super::*;

    pub fn dpapi_unprotect(_blob: &[u8]) -> Result<Vec<u8>, String> {
        Err("un archivo cifrado con DPAPI solo se puede leer en Windows".into())
    }

    pub fn run_elevated(_exe: &Path, _args: &str, _timeout: Duration) -> Result<(), ElevateError> {
        Err(ElevateError::Unsupported)
    }

    pub fn run_elevated_shown(_exe: &Path, _args: &str, _timeout: Duration) -> Result<(), ElevateError> {
        Err(ElevateError::Unsupported)
    }

    pub fn installed_role() -> Option<String> {
        None
    }

    pub fn autostart_get() -> Option<String> {
        None
    }

    pub fn autostart_set(_command: Option<&str>) -> Result<(), String> {
        Err("el inicio con la sesión solo se configura en Windows".into())
    }

    pub fn idle_time() -> Option<Duration> {
        None
    }

    pub fn wait_pid_exit(pid: u32, timeout: Duration) -> bool {
        let deadline = std::time::Instant::now() + timeout;
        while std::time::Instant::now() < deadline {
            let alive = std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .stderr(std::process::Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false);
            if !alive {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        false
    }
}

pub use imp::*;

/// Orden para `HKCU\…\Run`: el arrancador fijo si existe (la ruta del visor cambia con cada versión).
pub fn autostart_command(vmshost: Option<&Path>, current_exe: &Path) -> String {
    match vmshost.filter(|p| p.is_file()) {
        Some(host) => format!("\"{}\" viewer", host.display()),
        None => format!("\"{}\"", current_exe.display()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn autostart_prefers_vmshost() {
        let dir = tempfile::tempdir().unwrap();
        let host = dir.path().join("vmshost.exe");
        std::fs::write(&host, b"x").unwrap();
        let exe = Path::new("C:/x/versions/2.0.0/viewer/VMS.exe");
        assert_eq!(autostart_command(Some(&host), exe), format!("\"{}\" viewer", host.display()));
        assert_eq!(autostart_command(Some(&dir.path().join("no")), exe), format!("\"{}\"", exe.display()));
    }

    #[test]
    fn elevate_messages_are_spanish() {
        assert!(ElevateError::Cancelled.message_es().contains("administrador"));
        assert!(ElevateError::Failed(12).message_es().contains("salud"));
    }

    #[test]
    fn waiting_for_a_finished_process_returns() {
        let mut child = std::process::Command::new(if cfg!(windows) { "cmd" } else { "true" })
            .args(if cfg!(windows) { &["/C", "exit"][..] } else { &[][..] })
            .spawn()
            .unwrap();
        let pid = child.id();
        child.wait().unwrap();
        assert!(wait_pid_exit(pid, Duration::from_secs(5)));
    }
}
