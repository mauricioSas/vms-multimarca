//! Lectura y escritura del `.env` de la carpeta de datos (mismas claves que `vms.core.settings`).
//! Solo se leen los ajustes que `vmsctl` necesita: puertos, direcciones y HTTPS.

use std::collections::HashMap;
use std::io;
use std::path::Path;

/// `CLAVE=valor` (admite `export`, comillas y comentarios). Las claves repetidas: gana la última.
pub fn parse(text: &str) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((k, v)) = line.split_once('=') else { continue };
        let mut v = v.trim().to_string();
        if v.len() >= 2 && ((v.starts_with('"') && v.ends_with('"')) || (v.starts_with('\'') && v.ends_with('\''))) {
            v = v[1..v.len() - 1].to_string();
        } else if let Some(i) = v.find(" #") {
            v.truncate(i);
            v = v.trim_end().to_string();
        }
        out.insert(k.trim().to_string(), v);
    }
    out
}

pub fn read(path: &Path) -> HashMap<String, String> {
    std::fs::read_to_string(path).map(|t| parse(&t)).unwrap_or_default()
}

/// Fija `key=value` conservando el resto del archivo (sustituye la línea o la añade al final).
pub fn set(path: &Path, key: &str, value: &str) -> io::Result<()> {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let mut found = false;
    let mut lines: Vec<String> = text
        .lines()
        .map(|l| {
            let bare = l.trim_start().strip_prefix("export ").unwrap_or(l.trim_start());
            if bare.split_once('=').is_some_and(|(k, _)| k.trim() == key) {
                found = true;
                format!("{key}={value}")
            } else {
                l.to_string()
            }
        })
        .collect();
    if !found {
        lines.push(format!("{key}={value}"));
    }
    let mut out = lines.join("\n");
    out.push('\n');
    vms_common::atomic_write(path, out.as_bytes())
}

/// Fija `key=value` solo si la clave no está o está vacía. Devuelve `true` si la escribió.
pub fn set_default(path: &Path, key: &str, value: &str) -> io::Result<bool> {
    if read(path).get(key).is_some_and(|v| !v.is_empty()) {
        return Ok(false);
    }
    set(path, key, value)?;
    Ok(true)
}

/// Ajustes de red del `.env` con los mismos valores por defecto que `VmsSettings`.
#[derive(Clone, Debug)]
pub struct NetSettings {
    vars: HashMap<String, String>,
}

impl NetSettings {
    /// `.env` de la carpeta de datos; las variables `VMS_*` del proceso ganan (como en Python).
    pub fn load(env_file: &Path) -> Self {
        let mut vars = read(env_file);
        for (k, v) in std::env::vars() {
            if k.starts_with("VMS_") && !v.is_empty() {
                vars.insert(k, v);
            }
        }
        Self { vars }
    }

    #[cfg(test)]
    pub fn from_map(vars: HashMap<String, String>) -> Self {
        Self { vars }
    }

    fn get(&self, key: &str) -> Option<&str> {
        self.vars.get(key).map(String::as_str).filter(|v| !v.is_empty())
    }

    fn port(&self, key: &str, default: u16) -> u16 {
        self.get(key).and_then(|v| v.trim().parse().ok()).unwrap_or(default)
    }

    /// Dirección «host:puerto»; `None` si está desactivada (`off`, `none`, `disabled`, `-`).
    fn address(&self, key: &str, default: &str) -> Option<(String, u16)> {
        let v = self.get(key).unwrap_or(default).trim().to_string();
        if v.is_empty() || matches!(v.to_ascii_lowercase().as_str(), "off" | "none" | "disabled" | "-") {
            return None;
        }
        let (host, port) = v.rsplit_once(':')?;
        let host = host.trim_matches(['[', ']']);
        let host = if host.is_empty() { "0.0.0.0" } else { host };
        Some((host.to_string(), port.parse().ok()?))
    }

    pub fn http_port(&self) -> u16 {
        self.port("VMS_HTTP_PORT", 8600)
    }
    pub fn https_port(&self) -> u16 {
        self.port("VMS_HTTPS_PORT", 8643)
    }
    pub fn central_port(&self) -> u16 {
        self.port("VMS_CENTRAL_HTTP_PORT", 8700)
    }
    pub fn tls_enabled(&self) -> bool {
        self.get("VMS_TLS_CERT_FILE").is_some() && self.get("VMS_TLS_KEY_FILE").is_some()
    }
    pub fn ice_udp(&self) -> Option<(String, u16)> {
        self.address("VMS_MTX_WEBRTC_ICE_UDP", ":8189")
    }
    pub fn ice_tcp(&self) -> Option<(String, u16)> {
        self.address("VMS_MTX_WEBRTC_ICE_TCP", ":8189")
    }

    /// Puertos TCP de MediaMTX que solo escuchan en el equipo (RTSP, WHEP, API, reproducción, métricas).
    pub fn engine_tcp(&self) -> Vec<(String, u16)> {
        [
            ("VMS_MTX_RTSP_ADDRESS", "127.0.0.1:8554"),
            ("VMS_MTX_WEBRTC_ADDRESS", "127.0.0.1:8889"),
            ("VMS_MTX_API_ADDRESS", "127.0.0.1:9997"),
            ("VMS_MTX_PLAYBACK_ADDRESS", "127.0.0.1:9996"),
            ("VMS_MTX_METRICS_ADDRESS", "127.0.0.1:9998"),
        ]
        .iter()
        .filter_map(|(k, d)| self.address(k, d))
        .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_handles_quotes_comments_and_export() {
        let m = parse("# x\nVMS_HTTP_PORT=8700\nexport VMS_SITE_ID=\"tienda-1\"\nVMS_A=b # comentario\nmal\n");
        assert_eq!(m["VMS_HTTP_PORT"], "8700");
        assert_eq!(m["VMS_SITE_ID"], "tienda-1");
        assert_eq!(m["VMS_A"], "b");
        assert!(!m.contains_key("mal"));
    }

    #[test]
    fn set_replaces_or_appends_and_keeps_the_rest() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join(".env");
        std::fs::write(&p, "# cabecera\nVMS_TLS_CERT_FILE=viejo\nVMS_OTRA=1\n").unwrap();
        set(&p, "VMS_TLS_CERT_FILE", "C:\\datos\\secrets\\tls\\vms.crt").unwrap();
        set(&p, "VMS_TLS_KEY_FILE", "k").unwrap();
        let text = std::fs::read_to_string(&p).unwrap();
        assert_eq!(
            text,
            "# cabecera\nVMS_TLS_CERT_FILE=C:\\datos\\secrets\\tls\\vms.crt\nVMS_OTRA=1\nVMS_TLS_KEY_FILE=k\n"
        );
    }

    #[test]
    fn net_settings_defaults_and_off() {
        let s = NetSettings::from_map(HashMap::new());
        assert_eq!((s.http_port(), s.https_port(), s.central_port()), (8600, 8643, 8700));
        assert_eq!(s.ice_udp(), Some(("0.0.0.0".into(), 8189)));
        assert!(!s.tls_enabled());
        assert_eq!(s.engine_tcp().len(), 5);
        let s = NetSettings::from_map(
            [("VMS_MTX_WEBRTC_ICE_TCP", "off"), ("VMS_TLS_CERT_FILE", "a"), ("VMS_TLS_KEY_FILE", "b")]
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        );
        assert!(s.ice_tcp().is_none() && s.tls_enabled());
    }
}
