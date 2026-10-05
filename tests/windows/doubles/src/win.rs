//! Lo que los dobles hacen en Windows de verdad: elevación, servicios WinSW de la v1 y la ventana del visor.

use crate::Effects;
use std::ffi::c_void;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, HWND, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::Security::{GetTokenInformation, TokenElevation, TOKEN_ELEVATION, TOKEN_QUERY};
use windows_sys::Win32::System::Registry::{
    RegGetValueW, HKEY_LOCAL_MACHINE, RRF_NOEXPAND, RRF_RT_REG_EXPAND_SZ, RRF_RT_REG_SZ,
};
use windows_sys::Win32::System::Services::{
    CloseServiceHandle, ControlService, DeleteService, OpenSCManagerW, OpenServiceW, QueryServiceStatus,
    SC_MANAGER_CONNECT, SERVICE_CONTROL_STOP, SERVICE_QUERY_STATUS, SERVICE_STATUS, SERVICE_STOP, SERVICE_STOPPED,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

/// Servicios que registraba `install.ps1` de la v1 (WinSW).
pub const V1_SERVICES: [&str; 4] = ["VMSAnalytics", "VMSHeartbeat", "VMSCentral", "VMSBackend"];
const DELETE: u32 = 0x0001_0000;

pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

pub struct System;

impl Effects for System {
    fn elevated(&self) -> bool {
        // SAFETY: llamadas Win32 con punteros a variables locales del tamaño correcto; el token se cierra.
        unsafe {
            let mut token: HANDLE = std::ptr::null_mut();
            if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
                return false;
            }
            let mut elev = TOKEN_ELEVATION { TokenIsElevated: 0 };
            let mut len = 0u32;
            let ok = GetTokenInformation(
                token,
                TokenElevation,
                (&mut elev as *mut TOKEN_ELEVATION).cast::<c_void>(),
                std::mem::size_of::<TOKEN_ELEVATION>() as u32,
                &mut len,
            );
            CloseHandle(token);
            ok != 0 && elev.TokenIsElevated != 0
        }
    }

    fn user(&self) -> String {
        let domain = std::env::var("USERDOMAIN").unwrap_or_default();
        let user = std::env::var("USERNAME").unwrap_or_default();
        if domain.is_empty() {
            user
        } else {
            format!("{domain}\\{user}")
        }
    }

    fn remove_v1_services(&mut self) -> Result<Vec<String>, String> {
        let mut removed = Vec::new();
        for name in V1_SERVICES {
            let Some(image) = service_image_path(name) else {
                continue;
            };
            // Solo los de la v1 (WinSW en <instalación>\services\<nombre>.exe): nunca un servicio v2 o ajeno.
            let lower = image.to_lowercase();
            if !lower.contains(&format!("\\services\\{}.exe", name.to_lowercase())) {
                continue;
            }
            stop_and_delete(name)?;
            removed.push(name.to_string());
        }
        Ok(removed)
    }
}

fn service_image_path(name: &str) -> Option<String> {
    let key = wide(&format!("SYSTEM\\CurrentControlSet\\Services\\{name}"));
    let value = wide("ImagePath");
    let mut buf = vec![0u16; 4096];
    let mut size = (buf.len() * 2) as u32;
    // SAFETY: búfer propio con su tamaño en bytes; cadenas terminadas en cero.
    let rc = unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ | RRF_NOEXPAND,
            std::ptr::null_mut(),
            buf.as_mut_ptr().cast::<c_void>(),
            &mut size,
        )
    };
    if rc != 0 {
        return None;
    }
    let chars = (size as usize / 2).saturating_sub(1);
    Some(String::from_utf16_lossy(&buf[..chars.min(buf.len())]))
}

fn stop_and_delete(name: &str) -> Result<(), String> {
    let wname = wide(name);
    // SAFETY: manejadores del SCM abiertos y cerrados en este bloque; estructuras locales.
    unsafe {
        let scm = OpenSCManagerW(std::ptr::null(), std::ptr::null(), SC_MANAGER_CONNECT);
        if scm.is_null() {
            return Err(format!(
                "No se pudo abrir el administrador de servicios ({})",
                std::io::Error::last_os_error()
            ));
        }
        let svc = OpenServiceW(scm, wname.as_ptr(), SERVICE_STOP | SERVICE_QUERY_STATUS | DELETE);
        if svc.is_null() {
            CloseServiceHandle(scm);
            return Err(format!("No se pudo abrir el servicio {name} ({})", std::io::Error::last_os_error()));
        }
        let mut status: SERVICE_STATUS = std::mem::zeroed();
        ControlService(svc, SERVICE_CONTROL_STOP, &mut status);
        let deadline = Instant::now() + Duration::from_secs(60);
        while Instant::now() < deadline {
            if QueryServiceStatus(svc, &mut status) == 0 || status.dwCurrentState == SERVICE_STOPPED {
                break;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        let ok = DeleteService(svc);
        let err = std::io::Error::last_os_error();
        CloseServiceHandle(svc);
        CloseServiceHandle(scm);
        if ok == 0 {
            return Err(format!("No se pudo eliminar el servicio {name} ({err})"));
        }
    }
    Ok(())
}

// ================================================================================================ visor
pub mod viewer {
    use super::*;
    use windows_sys::Win32::Graphics::Gdi::{GetStockObject, HBRUSH, WHITE_BRUSH};
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DispatchMessageW, GetMessageW, LoadCursorW, PostQuitMessage, RegisterClassW,
        ShowWindow, TranslateMessage, CW_USEDEFAULT, IDC_ARROW, MSG, SW_SHOWNORMAL, WM_DESTROY, WNDCLASSW,
        WS_OVERLAPPEDWINDOW,
    };

    /// Clase de ventana que busca el e2e (paso 5).
    pub const CLASS: &str = "VMSVisorDePrueba";

    unsafe extern "system" fn wndproc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
        if msg == WM_DESTROY {
            PostQuitMessage(0);
            return 0;
        }
        DefWindowProcW(hwnd, msg, wparam, lparam)
    }

    /// Abre la ventana y espera a que la cierren (el e2e le manda WM_CLOSE).
    pub fn run(title: &str) -> i32 {
        let class = wide(CLASS);
        let title = wide(title);
        // SAFETY: registro de clase y bucle de mensajes estándar de Win32 con cadenas que viven todo el bloque.
        unsafe {
            let hinstance = GetModuleHandleW(std::ptr::null());
            let wc = WNDCLASSW {
                style: 0,
                lpfnWndProc: Some(wndproc),
                cbClsExtra: 0,
                cbWndExtra: 0,
                hInstance: hinstance,
                hIcon: std::ptr::null_mut(),
                hCursor: LoadCursorW(std::ptr::null_mut(), IDC_ARROW),
                hbrBackground: GetStockObject(WHITE_BRUSH) as HBRUSH,
                lpszMenuName: std::ptr::null(),
                lpszClassName: class.as_ptr(),
            };
            if RegisterClassW(&wc) == 0 {
                return 20;
            }
            let hwnd = CreateWindowExW(
                0,
                class.as_ptr(),
                title.as_ptr(),
                WS_OVERLAPPEDWINDOW,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                640,
                360,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                hinstance,
                std::ptr::null(),
            );
            if hwnd.is_null() {
                return 20;
            }
            ShowWindow(hwnd, SW_SHOWNORMAL);
            let mut msg: MSG = std::mem::zeroed();
            while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        }
        0
    }
}
