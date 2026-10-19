//! Aviso de actualización en la bandeja y reinicio del visor en la versión nueva (PLAN-V2 §2.5 «Visor»).
//!
//! El visor vigila `<datos>\updater\public-status.json` (CONTRATO §15.6, legible por Usuarios). Cuando la
//! versión instalada deja de ser la suya (actualización o vuelta atrás), se reinicia desde
//! `versions\<instalada>\viewer\VMS.exe`: los muros, de inmediato; si la persona está usando el panel, tras
//! 60 s sin actividad. «La suya» es la versión del producto (la carpeta `versions\<X>` de la que arranca), no la
//! del Cargo.toml del visor, que es `2.0.0-dev.0` en todas las versiones publicadas.

use std::cmp::Ordering;

use serde::{Deserialize, Serialize};
use vms_common::state::compare_versions;

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
pub struct PublicStatus {
    #[serde(default)]
    pub installed: Option<String>,
    #[serde(default)]
    pub channel: Option<String>,
    #[serde(default)]
    pub state: Option<String>,
    #[serde(default)]
    pub hold: Option<bool>,
    #[serde(default)]
    pub last_check: Option<String>,
    #[serde(default)]
    pub last_result: Option<String>,
    #[serde(default)]
    pub message_es: Option<String>,
    #[serde(default)]
    pub available: Option<String>,
    #[serde(default)]
    pub metadata_expires: Option<String>,
    #[serde(default)]
    pub clock_skew_s: Option<f64>,
    #[serde(default)]
    pub reboot_pending: Option<bool>,
    #[serde(default)]
    pub updated: Option<String>,
}

pub fn parse(bytes: &[u8]) -> Option<PublicStatus> {
    serde_json::from_slice(bytes).ok()
}

/// Estado que se muestra en el submenú «Actualizaciones».
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UpdateView {
    pub label: String,
    /// Versión a la que hay que reiniciar el visor (la instalada, distinta de la que corre).
    pub restart_into: Option<String>,
}

/// Precedencia SemVer (`2.0.0-beta.5` > `2.0.0-beta.4`, `2.0.0` > `2.0.0-rc.1`), la misma que el actualizador.
pub fn is_newer(candidate: &str, current: &str) -> bool {
    compare_versions(candidate, current) == Some(Ordering::Greater)
}

/// `own`: la versión del producto de este visor ([`crate::state::Viewer::version`]). Solo pide reiniciar el visor
/// si la instalada es de verdad otra versión: más nueva (actualización) o más antigua (vuelta atrás). Con la misma
/// versión, o si alguna no se puede comparar, no se reinicia por una duda.
pub fn view(own: &str, st: Option<&PublicStatus>) -> UpdateView {
    let Some(st) = st else {
        return UpdateView {
            label: format!("Versión {own} (estado de actualizaciones no disponible)"),
            restart_into: None,
        };
    };
    let installed = st.installed.as_deref().filter(|v| crate::paths::safe_version(v));
    if let Some(inst) = installed {
        let label = match compare_versions(inst, own) {
            Some(Ordering::Greater) => Some(format!("Instalada {inst}: reiniciar el visor")),
            Some(Ordering::Less) => Some(format!("Se volvió a {inst}: reiniciar el visor")),
            Some(Ordering::Equal) | None => None,
        };
        if let Some(label) = label {
            return UpdateView { label, restart_into: Some(inst.to_string()) };
        }
    }
    let current = installed.unwrap_or(own);
    let msg = st.message_es.as_deref().map(str::trim).filter(|m| !m.is_empty());
    let label = match st.last_result.as_deref() {
        Some("update_failed") => {
            format!("Falló la última actualización: {}", msg.unwrap_or("se volvió a la versión anterior"))
        }
        Some("clock_skew") => {
            msg.map(str::to_string).unwrap_or_else(|| "No se actualiza: la hora del equipo no es correcta".into())
        }
        Some("metadata_expired") => msg
            .map(str::to_string)
            .unwrap_or_else(|| "No se pudo comprobar si hay versiones nuevas (información caducada)".into()),
        _ if st.hold == Some(true) => format!("Al día {current} (actualizaciones retenidas)"),
        _ => match st.available.as_deref().filter(|a| is_newer(a, current)) {
            Some(a) => format!("Disponible {a}: se instalará en la ventana de mantenimiento"),
            None => format!("Al día {current}"),
        },
    };
    UpdateView { label, restart_into: None }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn st(json: &str) -> PublicStatus {
        parse(json.as_bytes()).expect("json")
    }

    #[test]
    fn contract_example_parses() {
        let s = st(r#"{"schema": 1, "installed": "2.1.0", "channel": "stable", "state": "good", "hold": false,
            "last_check": "2026-11-20T03:10:00Z", "last_result": "update_ok", "message_es": "", "available": null,
            "metadata_expires": "2026-11-27T00:00:00Z", "clock_skew_s": 0.4, "reboot_pending": false,
            "updated": "2026-11-20T03:10:05Z", "campo_futuro": 1}"#);
        assert_eq!(s.installed.as_deref(), Some("2.1.0"));
        assert!(parse(b"basura").is_none());
    }

    #[test]
    fn tray_labels() {
        let cases: &[(&str, &str, &str, Option<&str>)] = &[
            ("2.1.0", r#"{"installed": "2.1.0", "last_result": "update_ok"}"#, "Al día 2.1.0", None),
            ("2.1.0", r#"{"installed": "2.1.1"}"#, "Instalada 2.1.1: reiniciar el visor", Some("2.1.1")),
            (
                "2.1.1",
                r#"{"installed": "2.1.0", "last_result": "update_failed"}"#,
                "Se volvió a 2.1.0: reiniciar el visor",
                Some("2.1.0"),
            ),
            (
                "2.1.0",
                r#"{"installed": "2.1.0", "available": "2.2.0"}"#,
                "Disponible 2.2.0: se instalará en la ventana de mantenimiento",
                None,
            ),
            ("2.1.0", r#"{"installed": "2.1.0", "available": "2.0.0"}"#, "Al día 2.1.0", None),
            (
                "2.1.0",
                r#"{"installed": "2.1.0", "last_result": "update_failed", "message_es": "El backend no arrancó"}"#,
                "Falló la última actualización: El backend no arrancó",
                None,
            ),
            (
                "2.1.0",
                r#"{"installed": "2.1.0", "last_result": "clock_skew"}"#,
                "No se actualiza: la hora del equipo no es correcta",
                None,
            ),
            ("2.1.0", r#"{"installed": "2.1.0", "hold": true}"#, "Al día 2.1.0 (actualizaciones retenidas)", None),
            ("2.1.0", r#"{"installed": "../../x"}"#, "Al día 2.1.0", None),
            // betas: antes «beta.5» y «beta.4» eran iguales y una actualización salía como «Se volvió a…»
            (
                "2.0.0-beta.4",
                r#"{"installed": "2.0.0-beta.5"}"#,
                "Instalada 2.0.0-beta.5: reiniciar el visor",
                Some("2.0.0-beta.5"),
            ),
            (
                "2.0.0-beta.5",
                r#"{"installed": "2.0.0-beta.4"}"#,
                "Se volvió a 2.0.0-beta.4: reiniciar el visor",
                Some("2.0.0-beta.4"),
            ),
            (
                "2.0.0-beta.4",
                r#"{"installed": "2.0.0-beta.4", "available": "2.0.0-beta.5"}"#,
                "Disponible 2.0.0-beta.5: se instalará en la ventana de mantenimiento",
                None,
            ),
            // el estado real del PC del usuario (8/10/2026) con el visor de esa misma versión: nada que reiniciar
            (
                "2.0.0-beta.5",
                r#"{"installed": "2.0.0-beta.5", "channel": "stable", "state": "idle", "last_result": "error",
                    "message_es": "No hay fuente de actualizaciones configurada (VMS_UPDATE_SOURCE)"}"#,
                "Al día 2.0.0-beta.5",
                None,
            ),
            // la misma versión escrita de otra forma, o una que no es SemVer: no se reinicia por una duda
            ("2.0.0", r#"{"installed": "2.0.0+build.7"}"#, "Al día 2.0.0+build.7", None),
            ("2.0.0", r#"{"installed": "manual"}"#, "Al día manual", None),
        ];
        for (own, json, label, restart) in cases {
            let v = view(own, Some(&st(json)));
            assert_eq!(v.label, *label, "{json}");
            assert_eq!(v.restart_into.as_deref(), *restart, "{json}");
        }
        assert!(view("2.0.0", None).label.contains("no disponible"));
    }

    #[test]
    fn version_order() {
        assert!(is_newer("2.1.1", "2.1.0"));
        assert!(is_newer("2.10.0", "2.9.9"));
        assert!(is_newer("2.0.0", "2.0.0-dev.0"));
        assert!(!is_newer("2.0.0", "2.0.0"));
        assert!(!is_newer("1.9.0", "2.0.0"));
        assert!(is_newer("2.0.0-beta.5", "2.0.0-beta.4"));
        assert!(is_newer("2.0.0-beta.10", "2.0.0-beta.9"));
        assert!(!is_newer("2.0.0-beta.4", "2.0.0-beta.5"));
        assert!(is_newer("2.0.0", "2.0.0-rc.1"));
        assert!(!is_newer("2.0.0+build.7", "2.0.0"));
        assert!(!is_newer("manual", "2.0.0") && !is_newer("2.0.0", "manual"));
    }
}
