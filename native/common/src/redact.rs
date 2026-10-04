//! Ocultación de credenciales en textos de registro: mismas reglas que `vms.core.rtsp.redact`.
//!
//! Las dos implementaciones se comprueban con `tests/fixtures/redaction_vectors.json`
//! (pytest y `cargo test`). Si cambias una regla aquí, cámbiala también en Python y añade el vector.

use regex::Regex;
use std::borrow::Cow;
use std::sync::OnceLock;

struct Rules {
    url_cred: Regex,
    kv_secret: Regex,
    auth_header: Regex,
    telegram: Regex,
}

fn rules() -> &'static Rules {
    static RULES: OnceLock<Rules> = OnceLock::new();
    RULES.get_or_init(|| Rules {
        // usuario:contraseña@ en cualquier URL (rtsp, rtsps, http, https, postgresql...)
        url_cred: Regex::new(r#"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s"']+)@"#).expect("regex url"),
        // parámetros sensibles en query strings o pares clave=valor
        kv_secret: Regex::new(r#"(?i)\b(password|passwd|pass|pwd|token|api_key|apikey|secret)=([^&\s"']+)"#)
            .expect("regex kv"),
        // cabeceras Authorization
        auth_header: Regex::new(r"(?i)(authorization:\s*)(basic|digest|bearer)\s+[^\r\n]+").expect("regex auth"),
        // token de bot de Telegram en URLs
        telegram: Regex::new(r"(?i)(api\.telegram\.org/bot)[0-9]+:[A-Za-z0-9_\-]+").expect("regex telegram"),
    })
}

/// Oculta credenciales de URLs, parámetros y cabeceras dentro de un texto.
pub fn redact(text: &str) -> Cow<'_, str> {
    if text.is_empty() {
        return Cow::Borrowed(text);
    }
    let r = rules();
    let s = r.url_cred.replace_all(text, "${1}***:***@");
    let s = r.kv_secret.replace_all(&s, "${1}=***").into_owned();
    let s = r.auth_header.replace_all(&s, "${1}${2} ***").into_owned();
    let s = r.telegram.replace_all(&s, "${1}***").into_owned();
    if s == text {
        Cow::Borrowed(text)
    } else {
        Cow::Owned(s)
    }
}

#[cfg(test)]
mod tests {
    use super::redact;
    use std::path::PathBuf;

    fn vectors() -> serde_json::Value {
        let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/redaction_vectors.json");
        let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("no se pudo leer {}: {e}", p.display()));
        serde_json::from_str(&text).expect("JSON válido")
    }

    #[test]
    fn shared_vectors_match_python() {
        let v = vectors();
        let cases = v["cases"].as_array().expect("cases");
        assert!(!cases.is_empty());
        for c in cases {
            let name = c["name"].as_str().unwrap();
            let input = c["input"].as_str().unwrap();
            let expected = c["expected"].as_str().unwrap();
            assert_eq!(redact(input), expected, "vector «{name}»");
        }
    }

    #[test]
    fn no_secret_survives() {
        let v = vectors();
        for c in v["cases"].as_array().unwrap() {
            let out = redact(c["input"].as_str().unwrap()).into_owned();
            for s in v["secrets"].as_array().unwrap() {
                let s = s.as_str().unwrap();
                assert!(!out.contains(s), "«{}» deja ver un secreto", c["name"]);
            }
        }
    }
}
