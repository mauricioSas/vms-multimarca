//! Color de la bandeja según `GET /api/health` del servidor del panel (PLAN-V2 §2.3): verde, ámbar o rojo.

use serde::Deserialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    /// Todavía no se ha preguntado.
    Unknown,
    Ok,
    Degraded,
    /// El backend responde que está caído, o no responde.
    Down,
}

impl Level {
    pub fn tooltip(self) -> &'static str {
        match self {
            Self::Unknown => "VMS Multimarca · comprobando el servicio…",
            Self::Ok => "VMS Multimarca · todo en marcha",
            Self::Degraded => "VMS Multimarca · funcionando con avisos (mira «Estado del sistema»)",
            Self::Down => "VMS Multimarca · sin conexión con el servicio",
        }
    }
}

#[derive(Debug, Deserialize)]
struct HealthBody {
    status: String,
    #[serde(default)]
    version: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Health {
    pub level: Level,
    pub version: Option<String>,
}

pub fn parse(status: u16, body: &[u8]) -> Health {
    let parsed: Option<HealthBody> = serde_json::from_slice(body).ok();
    let level = match (status, parsed.as_ref().map(|b| b.status.as_str())) {
        (200, Some("ok")) => Level::Ok,
        (200, Some("degraded")) => Level::Degraded,
        (_, Some("down")) => Level::Down,
        (200, Some(_)) => Level::Degraded, // estado desconocido de una versión más nueva: avisar, no alarmar
        _ => Level::Down,
    };
    Health { level, version: parsed.and_then(|b| b.version) }
}

pub fn unreachable() -> Health {
    Health { level: Level::Down, version: None }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table() {
        let cases: &[(u16, &str, Level)] = &[
            (200, r#"{"status":"ok","version":"2.0.0"}"#, Level::Ok),
            (200, r#"{"status":"degraded"}"#, Level::Degraded),
            (200, r#"{"status":"down"}"#, Level::Down),
            (503, r#"{"status":"down"}"#, Level::Down),
            (200, r#"{"status":"mantenimiento"}"#, Level::Degraded),
            (200, "no es json", Level::Down),
            (500, "", Level::Down),
        ];
        for (code, body, want) in cases {
            assert_eq!(parse(*code, body.as_bytes()).level, *want, "{code} {body}");
        }
        assert_eq!(parse(200, br#"{"status":"ok","version":"2.0.0"}"#).version.as_deref(), Some("2.0.0"));
        assert!(Level::Down.tooltip().contains("sin conexión"));
    }
}
