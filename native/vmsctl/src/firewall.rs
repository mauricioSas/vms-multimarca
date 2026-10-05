//! `vmsctl firewall apply|remove` (PLAN-V2 §2.6): reglas de entrada **por puerto y perfil**, no por
//! programa (las rutas cambian con cada versión), con el prefijo `VMSMultimarca-` y nunca en el perfil
//! Público. Se usa `netsh advfirewall` y solo su código de salida (sin PowerShell y sin leer texto).
//! Las reglas aplicadas se anotan en `state\firewall.json` para poder quitarlas aunque cambien los puertos.

use crate::cli::CtlError;
use crate::envfile::NetSettings;
use crate::sys::{system32, Runner};
use serde_json::json;
use std::ffi::OsString;
use std::path::Path;
use vms_common::services::Role;

pub const PREFIX: &str = "VMSMultimarca-";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rule {
    pub name: String,
    pub proto: &'static str,
    pub port: u16,
}

fn rule(kind: &str, proto: &'static str, port: u16) -> Rule {
    Rule { name: format!("{PREFIX}{kind}-{proto}-{port}"), proto, port }
}

/// Reglas del puesto: web (HTTPS si hay certificado; si no, HTTP), ICE de WebRTC y panel central.
pub fn rules_for(role: Role, net: &NetSettings) -> Vec<Rule> {
    let mut out = Vec::new();
    if matches!(role, Role::Control | Role::Store) {
        if net.tls_enabled() {
            out.push(rule("WebHTTPS", "TCP", net.https_port()));
        } else {
            out.push(rule("Web", "TCP", net.http_port()));
        }
        if let Some((_, p)) = net.ice_udp() {
            out.push(rule("WebRTC", "UDP", p));
        }
        if let Some((_, p)) = net.ice_tcp() {
            out.push(rule("WebRTC", "TCP", p));
        }
    }
    if role == Role::Central {
        out.push(rule("Central", "TCP", net.central_port()));
    }
    out
}

/// `private[,domain]` → `private,domain`. El perfil Público nunca.
pub fn parse_profiles(s: &str) -> Result<String, CtlError> {
    let mut out: Vec<&str> = Vec::new();
    for p in s.split(',').map(str::trim).filter(|p| !p.is_empty()) {
        match p.to_ascii_lowercase().as_str() {
            "private" => out.push("private"),
            "domain" => out.push("domain"),
            "public" => {
                return Err(CtlError::usage(
                    "nunca se abren puertos en redes públicas: marca la red de la tienda como privada",
                ))
            }
            other => return Err(CtlError::usage(format!("perfil de red desconocido «{other}» (private o domain)"))),
        }
    }
    out.dedup();
    if out.is_empty() {
        return Err(CtlError::usage("indica al menos un perfil: --profiles private[,domain]"));
    }
    Ok(out.join(","))
}

fn netsh(runner: &mut dyn Runner, args: Vec<String>) -> Result<i32, CtlError> {
    let mut full: Vec<OsString> = vec!["advfirewall".into(), "firewall".into()];
    full.extend(args.into_iter().map(OsString::from));
    let r = runner.run(&system32("netsh.exe"), &full, &[]).map_err(|e| CtlError::io(&e, "netsh"))?;
    Ok(r.code)
}

fn recorded(state_file: &Path) -> Vec<String> {
    std::fs::read(state_file)
        .ok()
        .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
        .and_then(|v| v["rules"].as_array().cloned())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).filter(|n| n.starts_with(PREFIX)).collect())
        .unwrap_or_default()
}

/// Quita las reglas anotadas y las indicadas. `netsh … delete` da error si no existe: se comprueba
/// después con `show rule` (código 0 = sigue existiendo).
pub fn remove(runner: &mut dyn Runner, extra: &[String], state_file: &Path) -> Result<Vec<String>, CtlError> {
    let mut names = recorded(state_file);
    for n in extra {
        if !names.contains(n) {
            names.push(n.clone());
        }
    }
    for n in &names {
        netsh(runner, vec!["delete".into(), "rule".into(), format!("name={n}")])?;
        if netsh(runner, vec!["show".into(), "rule".into(), format!("name={n}")])? == 0 {
            return Err(CtlError::windows(format!("no se pudo quitar la regla del firewall «{n}»")));
        }
    }
    if state_file.exists() {
        std::fs::remove_file(state_file).map_err(|e| CtlError::io(&e, &state_file.display().to_string()))?;
    }
    Ok(names)
}

pub fn apply(runner: &mut dyn Runner, rules: &[Rule], profiles: &str, state_file: &Path) -> Result<(), CtlError> {
    let names: Vec<String> = rules.iter().map(|r| r.name.clone()).collect();
    remove(runner, &names, state_file)?;
    for r in rules {
        let code = netsh(
            runner,
            vec![
                "add".into(),
                "rule".into(),
                format!("name={}", r.name),
                "dir=in".into(),
                "action=allow".into(),
                format!("protocol={}", r.proto),
                format!("localport={}", r.port),
                format!("profile={profiles}"),
                "enable=yes".into(),
            ],
        )?;
        if code != 0 {
            return Err(CtlError::windows(format!("netsh no pudo crear la regla «{}» (código {code})", r.name)));
        }
    }
    let body = json!({"schema": 1, "profiles": profiles, "rules": names});
    vms_common::atomic_write(state_file, body.to_string().as_bytes())
        .map_err(|e| CtlError::io(&e, &state_file.display().to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sys::fake::FakeRunner;
    use std::collections::HashMap;

    fn net(pairs: &[(&str, &str)]) -> NetSettings {
        NetSettings::from_map(pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect::<HashMap<_, _>>())
    }

    #[test]
    fn rules_follow_role_and_https() {
        let names = |r: Vec<Rule>| r.into_iter().map(|r| r.name).collect::<Vec<_>>();
        assert_eq!(
            names(rules_for(Role::Store, &net(&[]))),
            ["VMSMultimarca-Web-TCP-8600", "VMSMultimarca-WebRTC-UDP-8189", "VMSMultimarca-WebRTC-TCP-8189"]
        );
        let tls = net(&[("VMS_TLS_CERT_FILE", "a"), ("VMS_TLS_KEY_FILE", "b"), ("VMS_MTX_WEBRTC_ICE_TCP", "off")]);
        assert_eq!(
            names(rules_for(Role::Control, &tls)),
            ["VMSMultimarca-WebHTTPS-TCP-8643", "VMSMultimarca-WebRTC-UDP-8189"]
        );
        assert_eq!(names(rules_for(Role::Central, &net(&[]))), ["VMSMultimarca-Central-TCP-8700"]);
        assert!(rules_for(Role::Viewer, &net(&[])).is_empty());
    }

    #[test]
    fn profiles_never_public() {
        assert_eq!(parse_profiles("private").unwrap(), "private");
        assert_eq!(parse_profiles("Private, domain").unwrap(), "private,domain");
        assert!(parse_profiles("public").is_err());
        assert!(parse_profiles("private,public").is_err());
        assert!(parse_profiles("").is_err());
    }

    #[test]
    fn apply_replaces_and_records_rules_then_remove_cleans() {
        let d = tempfile::tempdir().unwrap();
        let state = d.path().join("firewall.json");
        // `show rule` de una regla que ya no existe devuelve 1
        let mut r = FakeRunner { codes: vec![("show rule".into(), 1)], ..Default::default() };
        let rules = rules_for(Role::Control, &net(&[]));
        apply(&mut r, &rules, "private", &state).unwrap();
        let adds: Vec<&String> = r.calls.iter().filter(|c| c.contains(" add rule ")).collect();
        assert_eq!(adds.len(), 3);
        assert!(adds[0].ends_with(
            "name=VMSMultimarca-Web-TCP-8600 dir=in action=allow protocol=TCP localport=8600 profile=private enable=yes"
        ));
        assert_eq!(recorded(&state).len(), 3);
        r.calls.clear();
        let removed = remove(&mut r, &[], &state).unwrap();
        assert_eq!(removed.len(), 3);
        assert!(!state.exists());
        assert_eq!(r.calls.iter().filter(|c| c.contains(" delete rule ")).count(), 3);
    }

    #[test]
    fn a_rule_that_survives_delete_is_an_error() {
        let d = tempfile::tempdir().unwrap();
        let mut r = FakeRunner::default(); // show rule → 0: sigue ahí
        let e = remove(&mut r, &["VMSMultimarca-Web-TCP-8600".into()], &d.path().join("f.json")).unwrap_err();
        assert!(e.message.contains("no se pudo quitar"));
    }
}
