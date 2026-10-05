//! Archivos de secretos de `secrets\` (CONTRATO §13.9): DPAPI de máquina, con el mismo formato que
//! `vms.core.winsec` en Python.
//!
//! Formato en disco (texto ASCII, una línea): `vms-dpapi-v1:<base64 del blob de CryptProtectData>`.
//! Cualquier otro contenido es un secreto **en claro** de la v1 (se sigue leyendo para migrarlo).
//! El blob se crea con `CRYPTPROTECT_LOCAL_MACHINE` y una entropía fija del producto: cualquier
//! proceso del equipo puede descifrarlo, así que **la barrera real es la ACL** del archivo; DPAPI
//! añade que una copia del archivo fuera del equipo no sirve.

pub const PREFIX: &str = "vms-dpapi-v1:";
/// Entropía adicional fija (no es secreta; separa nuestros blobs de los de otras aplicaciones).
pub const ENTROPY: &[u8] = b"VMSMultimarca/secret/v1";

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const B64URL: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn encode_with(data: &[u8], table: &[u8; 64], pad: bool) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        let chars = [(n >> 18) & 63, (n >> 12) & 63, (n >> 6) & 63, n & 63];
        for (i, c) in chars.iter().enumerate() {
            if i <= chunk.len() {
                out.push(table[*c as usize] as char);
            } else if pad {
                out.push('=');
            }
        }
    }
    out
}

/// Base64 estándar con relleno (lo mismo que `base64.b64encode`).
pub fn b64encode(data: &[u8]) -> String {
    encode_with(data, B64, true)
}

/// Base64 para URL sin relleno (lo mismo que `secrets.token_urlsafe`).
pub fn b64url(data: &[u8]) -> String {
    encode_with(data, B64URL, false)
}

/// Base64 estándar (con o sin relleno). `None` si no es válido.
pub fn b64decode(text: &str) -> Option<Vec<u8>> {
    let clean: Vec<u8> = text.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    let body: Vec<u8> = clean.iter().copied().take_while(|b| *b != b'=').collect();
    if clean[body.len()..].iter().any(|b| *b != b'=') || body.len() % 4 == 1 {
        return None;
    }
    let mut out = Vec::with_capacity(body.len() * 3 / 4);
    let mut acc = 0u32;
    let mut bits = 0;
    for c in body {
        let v = B64.iter().position(|x| *x == c)? as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

/// Contenido de un archivo de secreto: protegido con DPAPI o en claro (v1).
#[derive(Debug, PartialEq, Eq)]
pub enum Stored {
    Protected(Vec<u8>),
    Plain(Vec<u8>),
}

pub fn parse(file: &[u8]) -> Stored {
    let text = String::from_utf8_lossy(file);
    let t = text.trim();
    if let Some(rest) = t.strip_prefix(PREFIX) {
        if let Some(blob) = b64decode(rest) {
            return Stored::Protected(blob);
        }
    }
    Stored::Plain(file.to_vec())
}

pub fn format_protected(blob: &[u8]) -> String {
    format!("{PREFIX}{}\n", b64encode(blob))
}

/// Bytes aleatorios del sistema (BCryptGenRandom en Windows, `/dev/urandom` en el resto).
pub fn random_bytes(n: usize) -> std::io::Result<Vec<u8>> {
    let mut buf = vec![0u8; n];
    #[cfg(windows)]
    {
        use windows_sys::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};
        // SAFETY: búfer propio de `n` bytes; algoritmo nulo con la bandera del generador del sistema.
        let st = unsafe {
            BCryptGenRandom(std::ptr::null_mut(), buf.as_mut_ptr(), n as u32, BCRYPT_USE_SYSTEM_PREFERRED_RNG)
        };
        if st != 0 {
            return Err(std::io::Error::other(format!("BCryptGenRandom falló (NTSTATUS {st:#x})")));
        }
    }
    #[cfg(not(windows))]
    {
        use std::io::Read;
        std::fs::File::open("/dev/urandom")?.read_exact(&mut buf)?;
    }
    Ok(buf)
}

/// DPAPI de máquina (solo Windows).
#[cfg(windows)]
pub mod dpapi {
    use super::ENTROPY;
    use std::io;
    use windows_sys::Win32::Foundation::{LocalFree, HLOCAL};
    use windows_sys::Win32::Security::Cryptography::{
        CryptProtectData, CryptUnprotectData, CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    fn blob(data: &[u8]) -> CRYPT_INTEGER_BLOB {
        CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 }
    }

    fn take(out: CRYPT_INTEGER_BLOB) -> Vec<u8> {
        // SAFETY: DPAPI devuelve un búfer de `cbData` bytes reservado con LocalAlloc; se copia y se libera.
        unsafe {
            let v = std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec();
            LocalFree(out.pbData as HLOCAL);
            v
        }
    }

    pub fn protect(data: &[u8]) -> io::Result<Vec<u8>> {
        let input = blob(data);
        let entropy = blob(ENTROPY);
        let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: std::ptr::null_mut() };
        // SAFETY: estructuras válidas durante la llamada; la salida se libera en `take`.
        let ok = unsafe {
            CryptProtectData(
                &input,
                std::ptr::null(),
                &entropy,
                std::ptr::null(),
                std::ptr::null(),
                CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(take(out))
    }

    pub fn unprotect(data: &[u8]) -> io::Result<Vec<u8>> {
        let input = blob(data);
        let entropy = blob(ENTROPY);
        let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: std::ptr::null_mut() };
        // SAFETY: como en `protect`.
        let ok = unsafe {
            CryptUnprotectData(
                &input,
                std::ptr::null_mut(),
                &entropy,
                std::ptr::null(),
                std::ptr::null(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(take(out))
    }
}

/// Lee un archivo de secreto y devuelve el secreto en claro (descifra con DPAPI si hace falta).
pub fn read_secret(path: &std::path::Path) -> std::io::Result<Vec<u8>> {
    let raw = std::fs::read(path)?;
    match parse(&raw) {
        Stored::Plain(p) => Ok(p),
        #[cfg(windows)]
        Stored::Protected(blob) => dpapi::unprotect(&blob),
        #[cfg(not(windows))]
        Stored::Protected(_) => {
            Err(std::io::Error::other("secreto protegido con DPAPI: solo se puede leer en el Windows que lo creó"))
        }
    }
}

/// Escribe un secreto: DPAPI de máquina en Windows; en claro en el resto (solo desarrollo).
pub fn write_secret(path: &std::path::Path, secret: &[u8]) -> std::io::Result<()> {
    #[cfg(windows)]
    let data = format_protected(&dpapi::protect(secret)?).into_bytes();
    #[cfg(not(windows))]
    let data = secret.to_vec();
    crate::atomic_write(path, &data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_python() {
        assert_eq!(b64encode(b""), "");
        assert_eq!(b64encode(b"f"), "Zg==");
        assert_eq!(b64encode(b"fo"), "Zm8=");
        assert_eq!(b64encode(b"foobar"), "Zm9vYmFy");
        assert_eq!(b64url(&[0xfb, 0xff, 0xfe]), "-__-");
        assert_eq!(b64url(&[0xfb, 0xff]), "-_8");
        for data in [&b""[..], b"a", b"ab", b"abc", &[0, 255, 128, 7, 9][..]] {
            assert_eq!(b64decode(&b64encode(data)).unwrap(), data);
        }
        assert!(b64decode("Zm9v!").is_none());
        assert!(b64decode("Z").is_none());
        assert!(b64decode("Zg==Zg").is_none());
    }

    #[test]
    fn parse_distinguishes_protected_and_plain() {
        assert_eq!(parse(b"vms-dpapi-v1:AQID\n"), Stored::Protected(vec![1, 2, 3]));
        assert_eq!(parse(b"un-token-de-la-v1"), Stored::Plain(b"un-token-de-la-v1".to_vec()));
        assert_eq!(parse(b"vms-dpapi-v1:%%%"), Stored::Plain(b"vms-dpapi-v1:%%%".to_vec()));
        assert_eq!(format_protected(&[1, 2, 3]), "vms-dpapi-v1:AQID\n");
    }

    #[test]
    fn random_bytes_are_random() {
        let a = random_bytes(32).unwrap();
        let b = random_bytes(32).unwrap();
        assert_eq!(a.len(), 32);
        assert_ne!(a, b);
    }

    #[cfg(windows)]
    #[test]
    fn dpapi_roundtrip_on_windows() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("kiosk.token");
        write_secret(&p, b"s3cr3t").unwrap();
        let raw = std::fs::read_to_string(&p).unwrap();
        assert!(raw.starts_with(PREFIX) && !raw.contains("s3cr3t"));
        assert_eq!(read_secret(&p).unwrap(), b"s3cr3t");
    }
}
