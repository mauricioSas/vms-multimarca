//! `vmsctl ports check` (PLAN-V2 §2.6): ¿están libres los puertos del puesto?
//!
//! Se comprueba **abriendo** cada puerto en la dirección que usará el servicio, sin leer texto de `netsh`:
//! - libre → se puede abrir;
//! - `in_use` → otro programa lo usa (`WSAEADDRINUSE`);
//! - `reserved` → Windows lo tiene reservado (`WSAEACCES`, típico de los rangos excluidos de Hyper-V/WSL,
//!   `netsh int ipv4 show excludedportrange`).

use crate::cli::{CtlError, Outcome};
use crate::envfile::NetSettings;
use serde::Serialize;
use serde_json::json;
use std::io;
use std::net::{TcpListener, UdpSocket};
use vms_common::exit_codes;
use vms_common::services::Role;

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PortCheck {
    pub host: String,
    pub port: u16,
    pub proto: &'static str,
    pub what: &'static str,
    pub status: &'static str,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub detail: String,
}

/// Puertos del puesto en la dirección donde escuchará cada servicio.
pub fn wanted(role: Role, net: &NetSettings) -> Vec<(String, u16, &'static str, &'static str)> {
    let mut out = Vec::new();
    if matches!(role, Role::Control | Role::Store) {
        out.push(("0.0.0.0".to_string(), net.http_port(), "tcp", "web"));
        out.push(("0.0.0.0".to_string(), net.https_port(), "tcp", "web HTTPS"));
        for (h, p) in net.engine_tcp() {
            out.push((h, p, "tcp", "motor (local)"));
        }
        if let Some((h, p)) = net.ice_udp() {
            out.push((h, p, "udp", "vídeo WebRTC"));
        }
        if let Some((h, p)) = net.ice_tcp() {
            out.push((h, p, "tcp", "vídeo WebRTC"));
        }
    }
    if role == Role::Central {
        out.push(("0.0.0.0".to_string(), net.central_port(), "tcp", "panel central"));
    }
    out
}

fn classify(e: &io::Error) -> &'static str {
    match (e.kind(), e.raw_os_error()) {
        (io::ErrorKind::AddrInUse, _) | (_, Some(10048)) => "in_use",
        (io::ErrorKind::PermissionDenied, _) | (_, Some(10013)) => "reserved",
        _ => "error",
    }
}

pub fn check_one(host: &str, port: u16, proto: &'static str, what: &'static str) -> PortCheck {
    let addr = format!("{host}:{port}");
    let res = if proto == "udp" { UdpSocket::bind(&addr).map(drop) } else { TcpListener::bind(&addr).map(drop) };
    let (status, detail) = match res {
        Ok(()) => ("free", String::new()),
        Err(e) => (classify(&e), e.to_string()),
    };
    PortCheck { host: host.to_string(), port, proto, what, status, detail }
}

pub fn check(role: Role, net: &NetSettings) -> Result<Outcome, CtlError> {
    let results: Vec<PortCheck> =
        wanted(role, net).into_iter().map(|(h, p, proto, what)| check_one(&h, p, proto, what)).collect();
    let bad: Vec<&PortCheck> = results.iter().filter(|r| r.status != "free").collect();
    let data = json!({"role": role.as_str(), "ports": results});
    if bad.is_empty() {
        return Ok(Outcome::new(data, format!("Los {} puertos del puesto están libres.", results.len())));
    }
    let lines: Vec<String> = bad
        .iter()
        .map(|b| match b.status {
            "reserved" => format!(
                "el puerto {}/{} ({}) está reservado por Windows (rango excluido de Hyper-V o WSL): cámbialo en el .env",
                b.port, b.proto, b.what
            ),
            "in_use" => format!("el puerto {}/{} ({}) está ocupado por otro programa", b.port, b.proto, b.what),
            _ => format!("el puerto {}/{} ({}) no se puede abrir: {}", b.port, b.proto, b.what, b.detail),
        })
        .collect();
    Err(CtlError::new(exit_codes::PORT_IN_USE, "port_in_use", lines.join("; ")).with_data(data))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn busy_port_is_reported_with_code_10() {
        let busy = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = busy.local_addr().unwrap().port();
        let c = check_one("127.0.0.1", port, "tcp", "web");
        assert_eq!(c.status, "in_use");
        drop(busy);
        assert_eq!(check_one("127.0.0.1", port, "tcp", "web").status, "free");

        let net = NetSettings::from_map(
            [("VMS_MTX_API_ADDRESS".to_string(), format!("127.0.0.1:{port}"))].into_iter().collect::<HashMap<_, _>>(),
        );
        let _busy = TcpListener::bind(format!("127.0.0.1:{port}")).unwrap();
        let e = check(Role::Control, &net).unwrap_err();
        assert_eq!(e.exit, exit_codes::PORT_IN_USE);
        assert!(e.message.contains(&port.to_string()), "{}", e.message);
    }

    #[test]
    fn wanted_ports_by_role() {
        let net = NetSettings::from_map(HashMap::new());
        let ports: Vec<u16> = wanted(Role::Store, &net).iter().map(|w| w.1).collect();
        for p in [8600, 8643, 8554, 8889, 9997, 9996, 9998, 8189] {
            assert!(ports.contains(&p), "falta {p}");
        }
        assert_eq!(wanted(Role::Central, &net).iter().map(|w| w.1).collect::<Vec<_>>(), [8700]);
        assert!(wanted(Role::Viewer, &net).is_empty());
    }
}
