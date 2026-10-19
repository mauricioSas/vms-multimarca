//! Entrada de los muros sin escribir contraseñas (CONTRATO §17.1).
//!
//! `ProgramData\VMSMultimarca\secrets\kiosk.token` solo lo pueden leer SYSTEM, Administradores, el servicio y
//! el grupo local `VMS Operadores`. El visor lo lee al abrir un muro del servidor local y lo cambia por la
//! cookie de kiosco con `POST /api/local/kiosk-session` **desde la propia página del backend** (la petición
//! la hace el WebView, así la cookie queda en su almacén). El token no pasa nunca por la línea de órdenes, la
//! URL, el registro ni las páginas locales.
//!
//! Formato del archivo (CONTRATO §13.9): el de todos los secretos del producto, que escriben `vmsctl kiosk
//! rotate`, el instalador y el backend (`vms_common::secret` y `vms.core.winsec`): una línea
//! `vms-dpapi-v1:<base64>` con un blob DPAPI de máquina creado con la entropía fija del producto. Se descifra
//! con `vms_common::secret::dpapi`, que usa esa misma entropía. También vale el token en claro de la v1 (UTF-8;
//! se ignoran BOM y espacios). Una línea con el prefijo que no se puede descifrar es un error: nunca se manda
//! tal cual como si fuera el token (el servicio la rechazaría con 401 y el muro no abriría nunca).

use std::io::{self, Read};
use std::path::Path;

use vms_common::secret::{self, Stored};

const MAX_FILE: u64 = 64 * 1024;
const UTF8_BOM: &[u8] = b"\xEF\xBB\xBF";

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

/// Lee la clave de los muros. Solo devuelve `Ok` con un token ya descifrado y con formato válido: es lo que se
/// manda al servicio. Los mensajes de error nunca llevan el contenido del archivo.
pub fn read_token(path: &Path) -> Result<Token, TokenError> {
    let mut f = std::fs::File::open(path).map_err(map_io)?;
    let mut raw = Vec::new();
    f.by_ref().take(MAX_FILE + 1).read_to_end(&mut raw).map_err(map_io)?;
    if raw.len() as u64 > MAX_FILE {
        zero(&mut raw);
        return Err(TokenError::Invalid("archivo demasiado grande".into()));
    }
    let plain = decode(&raw);
    zero(&mut raw);
    let mut plain = plain?;
    let token = parse_token(&plain);
    zero(&mut plain);
    token
}

/// Contenido del archivo → token en claro: descifra el formato protegido o devuelve el texto de la v1.
fn decode(raw: &[u8]) -> Result<Vec<u8>, TokenError> {
    let body = raw.strip_prefix(UTF8_BOM).unwrap_or(raw);
    match secret::parse(body) {
        Stored::Protected(mut blob) => {
            let plain = unprotect(&blob);
            zero(&mut blob);
            plain
        }
        // `secret::parse` devuelve como «en claro» una línea con el prefijo pero con base64 roto
        Stored::Plain(mut p) if p.trim_ascii_start().starts_with(secret::PREFIX.as_bytes()) => {
            zero(&mut p);
            Err(TokenError::Invalid("está cifrada, pero el texto cifrado está dañado (no es base64 válido)".into()))
        }
        Stored::Plain(p) => Ok(p),
    }
}

#[cfg(windows)]
fn unprotect(blob: &[u8]) -> Result<Vec<u8>, TokenError> {
    secret::dpapi::unprotect(blob)
        .map_err(|e| TokenError::Invalid(format!("Windows no pudo descifrarla en este equipo ({e})")))
}

#[cfg(not(windows))]
fn unprotect(_blob: &[u8]) -> Result<Vec<u8>, TokenError> {
    Err(TokenError::Invalid("está cifrada con DPAPI y solo se puede descifrar en el Windows que la creó".into()))
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

    /// El fallo real del 8/10/2026: `kiosk.token` lo escribe `vmsctl` con el formato protegido
    /// (`vms-dpapi-v1:<base64>`, DPAPI de máquina con la entropía del producto) y el visor mandaba la línea
    /// entera como token. El servicio respondía 401 y los muros no abrían nunca. Aquí, DPAPI de verdad.
    #[cfg(windows)]
    #[test]
    fn reads_the_token_that_vmsctl_writes_with_dpapi() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kiosk.token");
        let token = secret::b64url(&secret::random_bytes(32).unwrap()); // como `vmsctl kiosk rotate`
        secret::write_secret(&p, token.as_bytes()).unwrap();
        let on_disk = std::fs::read_to_string(&p).unwrap();
        assert!(on_disk.starts_with(secret::PREFIX) && !on_disk.contains(&token), "el archivo va cifrado");
        assert_eq!(read_token(&p).unwrap().expose(), token);
        // con BOM y saltos de línea de Windows también
        std::fs::write(&p, format!("\u{feff}{}\r\n", on_disk.trim())).unwrap();
        assert_eq!(read_token(&p).unwrap().expose(), token);
    }

    /// Una línea `vms-dpapi-v1:…` que no se puede descifrar (blob de otro equipo, base64 roto) es un error, en
    /// cualquier sistema: nunca se manda como token ni sale en el mensaje.
    #[test]
    fn a_protected_line_is_never_sent_as_the_token() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kiosk.token");
        for contents in [
            "vms-dpapi-v1:AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA=\n", // base64 válido, blob que no es DPAPI
            "vms-dpapi-v1:%%%no-es-base64%%%-1234567890\n",
            "\u{feff}  vms-dpapi-v1:AQIDBAUGBwgJCgsMDQ4PEBESExQ=\r\n",
        ] {
            std::fs::write(&p, contents).unwrap();
            match read_token(&p) {
                Ok(t) => panic!("se aceptó como token una línea cifrada ({} caracteres)", t.expose().len()),
                Err(e) => {
                    assert!(matches!(e, TokenError::Invalid(_)), "{contents:?}: {e:?}");
                    assert_eq!(e.code(), "token_invalido");
                    let msg = e.to_string();
                    assert!(!msg.contains("AQID") && !msg.contains("%%%"), "el mensaje no copia el archivo: {msg}");
                }
            }
        }
    }

    /// Un blob binario suelto (sin el prefijo) no es texto: no se intenta descifrar ni se manda.
    #[test]
    fn a_raw_binary_blob_is_not_a_token() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kiosk.token");
        let mut blob = vec![1u8, 0, 0, 0, 0xd0, 0x8c, 0x9d, 0xdf, 0x01, 0x15, 0xd1, 0x11];
        blob.extend_from_slice(&[0xffu8; 64]);
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
