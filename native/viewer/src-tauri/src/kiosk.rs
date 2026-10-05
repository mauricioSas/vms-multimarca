//! Entrada de los muros sin escribir contraseñas (CONTRATO §17.1).
//!
//! `ProgramData\VMSMultimarca\secrets\kiosk.token` solo lo pueden leer SYSTEM, Administradores, el servicio y
//! el grupo local `VMS Operadores`. El visor lo lee al abrir un muro del servidor local y lo cambia por la
//! cookie de kiosco con `POST /api/local/kiosk-session` **desde la propia página del backend** (la petición
//! la hace el WebView, así la cookie queda en su almacén). El token no pasa nunca por la línea de órdenes, la
//! URL, el registro ni las páginas locales.
//!
//! Formato del archivo: el token en texto (UTF-8, se ignoran BOM y espacios) o, si B1 lo guarda cifrado, un
//! blob DPAPI de máquina (se reconoce por su cabecera y se descifra con `CryptUnprotectData`).

use std::io::{self, Read};
use std::path::Path;

const MAX_FILE: u64 = 64 * 1024;
/// Cabecera de un blob DPAPI: versión 1 + GUID del proveedor {df9d8cd0-1501-11d1-8c7a-00c04fc297eb}.
const DPAPI_HEADER: [u8; 20] =
    [1, 0, 0, 0, 0xd0, 0x8c, 0x9d, 0xdf, 0x01, 0x15, 0xd1, 0x11, 0x8c, 0x7a, 0x00, 0xc0, 0x4f, 0xc2, 0x97, 0xeb];

/// Token en memoria: no se imprime con `{:?}` y se borra al soltarlo.
pub struct Token(String);

impl Token {
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for Token {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Token(***)")
    }
}

impl Drop for Token {
    fn drop(&mut self) {
        // SAFETY: se escriben ceros (UTF-8 válido) sobre los mismos bytes antes de liberar.
        unsafe {
            for b in self.0.as_bytes_mut() {
                std::ptr::write_volatile(b, 0);
            }
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum TokenError {
    /// El usuario no está en `VMS Operadores` (la ACL no le deja leer el archivo).
    PermissionDenied,
    NotFound,
    Invalid(String),
    Io(String),
}

impl TokenError {
    /// Código estable para la página `sin-permiso.html`.
    pub fn code(&self) -> &'static str {
        match self {
            Self::PermissionDenied => "sin_permiso",
            Self::NotFound => "sin_token",
            Self::Invalid(_) => "token_invalido",
            Self::Io(_) => "error_lectura",
        }
    }
}

impl std::fmt::Display for TokenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::PermissionDenied => f.write_str("Sin permiso para abrir los muros"),
            Self::NotFound => f.write_str("Este equipo no tiene la clave de los muros"),
            Self::Invalid(m) => write!(f, "La clave de los muros no es válida: {m}"),
            Self::Io(m) => write!(f, "No se pudo leer la clave de los muros: {m}"),
        }
    }
}

fn map_io(e: io::Error) -> TokenError {
    match e.kind() {
        io::ErrorKind::PermissionDenied => TokenError::PermissionDenied,
        io::ErrorKind::NotFound => TokenError::NotFound,
        _ => TokenError::Io(e.to_string()),
    }
}

pub fn read_token(path: &Path) -> Result<Token, TokenError> {
    let mut f = std::fs::File::open(path).map_err(map_io)?;
    let mut raw = Vec::new();
    f.by_ref().take(MAX_FILE + 1).read_to_end(&mut raw).map_err(map_io)?;
    if raw.len() as u64 > MAX_FILE {
        return Err(TokenError::Invalid("archivo demasiado grande".into()));
    }
    let text_bytes = if raw.starts_with(&DPAPI_HEADER) {
        let plain = crate::platform::dpapi_unprotect(&raw).map_err(TokenError::Invalid)?;
        zero(&mut raw);
        plain
    } else {
        raw
    };
    let token = parse_token(&text_bytes);
    let mut text_bytes = text_bytes;
    zero(&mut text_bytes);
    token
}

fn zero(v: &mut [u8]) {
    for b in v.iter_mut() {
        // SAFETY: puntero válido a un byte del propio vector
        unsafe { std::ptr::write_volatile(b, 0) };
    }
}

fn parse_token(bytes: &[u8]) -> Result<Token, TokenError> {
    let text = std::str::from_utf8(bytes).map_err(|_| TokenError::Invalid("no es texto UTF-8".into()))?;
    let t = text.trim_start_matches('\u{feff}').trim();
    if t.len() < 16 {
        return Err(TokenError::Invalid("demasiado corta".into()));
    }
    if t.len() > 512 || !t.chars().all(|c| c.is_ascii_graphic()) {
        return Err(TokenError::Invalid("caracteres no admitidos".into()));
    }
    Ok(Token(t.to_string()))
}

/// Ruta de la página del backend donde se hace el intercambio (router `local`, B2).
pub const EXCHANGE_PAGE: &str = "/api/local/kiosk";
pub const EXCHANGE_API: &str = "/api/local/kiosk-session";

/// Script que el visor ejecuta en `EXCHANGE_PAGE` (mismo origen que el backend): pide la cookie de kiosco y
/// abre el muro. Si falla, vuelve a la página con `?error=<código>` (que ya no repite el intercambio).
pub fn exchange_script(token: &Token, next: &str) -> String {
    let token_js = serde_json::to_string(token.expose()).expect("cadena JSON");
    let next_js = serde_json::to_string(next).expect("cadena JSON");
    format!(
        r#"(async () => {{
  const next = {next_js};
  let code = "red";
  try {{
    const r = await fetch({api:?}, {{
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {{"Content-Type": "application/json", "X-Requested-With": "vms"}},
      body: JSON.stringify({{token: {token_js}, next}})
    }});
    if (r.status === 204) {{ location.replace(next); return; }}
    code = String(r.status);
  }} catch (e) {{ code = "red"; }}
  location.replace({page:?} + "?error=" + encodeURIComponent(code) + "&next=" + encodeURIComponent(next));
}})();"#,
        api = EXCHANGE_API,
        page = EXCHANGE_PAGE,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_plain_token_with_bom_and_newline() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kiosk.token");
        std::fs::write(&p, "\u{feff}  token-de-kiosco-1234567890\r\n").unwrap();
        assert_eq!(read_token(&p).unwrap().expose(), "token-de-kiosco-1234567890");
    }

    #[test]
    fn missing_file_and_bad_contents() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kiosk.token");
        assert_eq!(read_token(&p).unwrap_err(), TokenError::NotFound);
        std::fs::write(&p, "corto").unwrap();
        assert!(matches!(read_token(&p), Err(TokenError::Invalid(_))));
        std::fs::write(&p, "con espacios en medio 1234567890").unwrap();
        assert!(matches!(read_token(&p), Err(TokenError::Invalid(_))));
        std::fs::write(&p, [0xff, 0xfe, 0x00, 0x41]).unwrap();
        assert!(matches!(read_token(&p), Err(TokenError::Invalid(_))));
    }

    /// Un usuario fuera de `VMS Operadores`: el sistema le niega la lectura → «sin permiso». En Windows lo
    /// prueba de verdad la prueba de humo de CI (ACL de denegación); aquí, con permisos POSIX.
    #[cfg(unix)]
    #[test]
    fn permission_denied_maps_to_sin_permiso() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kiosk.token");
        std::fs::write(&p, "token-de-kiosco-1234567890").unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o000)).unwrap();
        let readable_anyway = std::fs::read(&p).is_ok(); // root lo lee todo
        let r = read_token(&p);
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600)).unwrap();
        if !readable_anyway {
            let e = r.unwrap_err();
            assert_eq!(e, TokenError::PermissionDenied);
            assert_eq!(e.code(), "sin_permiso");
            assert_eq!(e.to_string(), "Sin permiso para abrir los muros");
        }
    }

    #[cfg(not(windows))]
    #[test]
    fn dpapi_blob_is_recognized_but_not_decryptable_outside_windows() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kiosk.token");
        let mut blob = DPAPI_HEADER.to_vec();
        blob.extend_from_slice(&[0u8; 64]);
        std::fs::write(&p, blob).unwrap();
        assert!(matches!(read_token(&p), Err(TokenError::Invalid(_))));
    }

    #[test]
    fn token_never_shows_in_debug() {
        let t = Token("secreto-secreto-secreto".into());
        assert_eq!(format!("{t:?}"), "Token(***)");
    }

    #[test]
    fn exchange_script_escapes_and_targets_same_origin() {
        let t = Token(r#"ab"c\d</script>-1234567890"#.into());
        let js = exchange_script(&t, "/wall/2");
        assert!(js.contains(r#""ab\"c\\d</script>-1234567890""#), "{js}");
        assert!(js.contains(r#"fetch("/api/local/kiosk-session""#));
        assert!(js.contains(r#""X-Requested-With": "vms""#));
        assert!(js.contains(r#"const next = "/wall/2";"#));
        assert!(!js.contains("http://"), "la petición va al mismo origen");
    }
}
