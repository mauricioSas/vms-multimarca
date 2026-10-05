//! Registro de Windows (solo lo imprescindible):
//! - `HKLM\SOFTWARE\VMSMultimarca`: `InstallDir`, `DataDir` y `Role` (los escribe `services install`).
//! - `HKLM\SYSTEM\CurrentControlSet\Services\<Servicio>\Environment`: `VMS_DATA_DIR` para cada servicio
//!   (así `vmshost`, `vmsctl run` y Python ven la misma carpeta de datos sin tocar la línea de órdenes).

use std::io;
use windows_sys::Win32::Foundation::ERROR_FILE_NOT_FOUND;
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegCreateKeyExW, RegDeleteTreeW, RegGetValueW, RegSetValueExW, HKEY, HKEY_LOCAL_MACHINE,
    KEY_WOW64_64KEY, KEY_WRITE, REG_MULTI_SZ, REG_OPTION_NON_VOLATILE, REG_SZ, RRF_RT_REG_SZ, RRF_SUBKEY_WOW6464KEY,
};

pub const PRODUCT_KEY: &str = r"SOFTWARE\VMSMultimarca";

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn check(code: u32) -> io::Result<()> {
    if code == 0 {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(code as i32))
    }
}

struct Key(HKEY);

impl Drop for Key {
    fn drop(&mut self) {
        // SAFETY: clave abierta por nosotros.
        unsafe { RegCloseKey(self.0) };
    }
}

fn create(subkey: &str) -> io::Result<Key> {
    let mut h: HKEY = std::ptr::null_mut();
    let sk = wide(subkey);
    // SAFETY: cadenas UTF-16 terminadas en cero; `h` recibe la clave.
    let code = unsafe {
        RegCreateKeyExW(
            HKEY_LOCAL_MACHINE,
            sk.as_ptr(),
            0,
            std::ptr::null(),
            REG_OPTION_NON_VOLATILE,
            KEY_WRITE | KEY_WOW64_64KEY,
            std::ptr::null(),
            &mut h,
            std::ptr::null_mut(),
        )
    };
    check(code)?;
    Ok(Key(h))
}

fn set_raw(subkey: &str, name: &str, kind: u32, data: &[u16]) -> io::Result<()> {
    let key = create(subkey)?;
    let n = wide(name);
    // SAFETY: búfer UTF-16 con su tamaño en bytes.
    let code =
        unsafe { RegSetValueExW(key.0, n.as_ptr(), 0, kind, data.as_ptr() as *const u8, (data.len() * 2) as u32) };
    check(code)
}

pub fn set_string(subkey: &str, name: &str, value: &str) -> io::Result<()> {
    set_raw(subkey, name, REG_SZ, &wide(value))
}

/// REG_MULTI_SZ: cadenas terminadas en cero y un cero final.
pub fn set_multi(subkey: &str, name: &str, values: &[String]) -> io::Result<()> {
    let mut data: Vec<u16> = Vec::new();
    for v in values {
        data.extend(v.encode_utf16());
        data.push(0);
    }
    data.push(0);
    set_raw(subkey, name, REG_MULTI_SZ, &data)
}

pub fn get_string(subkey: &str, name: &str) -> io::Result<Option<String>> {
    let sk = wide(subkey);
    let n = wide(name);
    let mut size: u32 = 0;
    // SAFETY: primera llamada para conocer el tamaño.
    let code = unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            sk.as_ptr(),
            n.as_ptr(),
            RRF_RT_REG_SZ | RRF_SUBKEY_WOW6464KEY,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if code == ERROR_FILE_NOT_FOUND {
        return Ok(None);
    }
    check(code)?;
    let mut buf = vec![0u16; (size as usize).div_ceil(2) + 1];
    let mut size = (buf.len() * 2) as u32;
    // SAFETY: búfer del tamaño indicado.
    let code = unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            sk.as_ptr(),
            n.as_ptr(),
            RRF_RT_REG_SZ | RRF_SUBKEY_WOW6464KEY,
            std::ptr::null_mut(),
            buf.as_mut_ptr() as *mut core::ffi::c_void,
            &mut size,
        )
    };
    check(code)?;
    let len = buf.iter().position(|c| *c == 0).unwrap_or(buf.len());
    Ok(Some(String::from_utf16_lossy(&buf[..len])))
}

/// Borra una clave y todo lo que tiene debajo (no es error si no existe).
pub fn delete_tree(subkey: &str) -> io::Result<()> {
    let sk = wide(subkey);
    // SAFETY: cadena UTF-16 terminada en cero.
    let code = unsafe { RegDeleteTreeW(HKEY_LOCAL_MACHINE, sk.as_ptr()) };
    if code == ERROR_FILE_NOT_FOUND {
        return Ok(());
    }
    check(code)
}

pub fn service_key(service: &str) -> String {
    format!(r"SYSTEM\CurrentControlSet\Services\{service}")
}
