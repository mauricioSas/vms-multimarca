//! `vmsctl health wait --timeout 120 [--deep]`: espera a que el puesto esté sano (sale con 0) o falla con
//! el código 12 y el motivo.
//!
//! Comprueba: los servicios instalados están en marcha (SCM) y, según el puesto, `GET /api/health` del
//! backend (`status` distinto de `down`) y del panel central. Con `--deep` usa
//! `GET /api/internal/health/deep` con el token interno (PLAN-V2 §2.5 paso 7); si esa ruta aún no existe
//! en la versión instalada (404), lo dice y usa la normal.

use crate::cli::{CtlError, Outcome};
use crate::envfile::NetSettings;
use crate::scm::{Scm, SvcState};
use serde_json::{json, Value};
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant};
use vms_common::layout::DataLayout;
use vms_common::services::{BACKEND, CENTRAL};

/// GET HTTP/1.1 mínimo (sin dependencias). Devuelve el código y el cuerpo (con o sin «chunked»).
pub fn http_get(
    host: &str,
    port: u16,
    path: &str,
    headers: &[(&str, &str)],
    timeout: Duration,
) -> io::Result<(u16, Vec<u8>)> {
    let addr: SocketAddr = (host, port)
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "dirección no válida"))?;
    let mut s = TcpStream::connect_timeout(&addr, timeout)?;
    s.set_read_timeout(Some(timeout))?;
    s.set_write_timeout(Some(timeout))?;
    let mut req =
        format!("GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\nAccept: application/json\r\n");
    for (k, v) in headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    req.push_str("\r\n");
    s.write_all(req.as_bytes())?;
    let mut raw = Vec::new();
    s.read_to_end(&mut raw)?;
    parse_response(&raw)
}

fn parse_response(raw: &[u8]) -> io::Result<(u16, Vec<u8>)> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, "respuesta HTTP no válida");
    let split = raw.windows(4).position(|w| w == b"\r\n\r\n").ok_or_else(bad)?;
    let head = String::from_utf8_lossy(&raw[..split]);
    let mut lines = head.lines();
    let status: u16 =
        lines.next().and_then(|l| l.split_whitespace().nth(1)).and_then(|c| c.parse().ok()).ok_or_else(bad)?;
    let chunked = lines.any(|l| {
        let l = l.to_ascii_lowercase();
        l.starts_with("transfer-encoding:") && l.contains("chunked")
    });
    let body = &raw[split + 4..];
    if !chunked {
        return Ok((status, body.to_vec()));
    }
    let mut out = Vec::new();
    let mut rest = body;
    loop {
        let eol = rest.windows(2).position(|w| w == b"\r\n").ok_or_else(bad)?;
        let size_txt = String::from_utf8_lossy(&rest[..eol]);
        let size = usize::from_str_radix(size_txt.split(';').next().unwrap_or("").trim(), 16).map_err(|_| bad())?;
        rest = &rest[eol + 2..];
        if size == 0 {
            return Ok((status, out));
        }
        if rest.len() < size + 2 {
            return Err(bad());
        }
        out.extend_from_slice(&rest[..size]);
        rest = &rest[size + 2..];
    }
}

#[derive(Debug)]
struct Check {
    name: String,
    ok: bool,
    detail: String,
}

fn backend_check(net: &NetSettings, data: &DataLayout, deep: bool, deep_missing: &mut bool) -> Check {
    let port = net.http_port();
    let token = if deep && !*deep_missing {
        vms_common::secret::read_secret(&data.secrets_dir().join("internal.token"))
            .ok()
            .map(|t| String::from_utf8_lossy(&t).trim().to_string())
    } else {
        None
    };
    let (path, headers): (&str, Vec<(&str, &str)>) = match &token {
        Some(t) => ("/api/internal/health/deep", vec![("x-vms-internal-token", t.as_str())]),
        None => ("/api/health", vec![]),
    };
    match http_get("127.0.0.1", port, path, &headers, Duration::from_secs(5)) {
        Ok((404, _)) if token.is_some() => {
            *deep_missing = true;
            Check { name: "backend".into(), ok: false, detail: "sin /api/internal/health/deep en esta versión".into() }
        }
        Ok((200, body)) => {
            let v: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
            let status = v["status"].as_str().unwrap_or("").to_string();
            let ok = !status.is_empty() && status != "down";
            Check { name: "backend".into(), ok, detail: format!("status={status} engine={}", v["engine"]) }
        }
        Ok((code, _)) => Check { name: "backend".into(), ok: false, detail: format!("HTTP {code} en {path}") },
        Err(e) => Check { name: "backend".into(), ok: false, detail: format!("127.0.0.1:{port} no responde ({e})") },
    }
}

fn central_check(net: &NetSettings) -> Check {
    let port = net.central_port();
    match http_get("127.0.0.1", port, "/api/health", &[], Duration::from_secs(5)) {
        Ok((200, _)) => Check { name: "central".into(), ok: true, detail: "HTTP 200".into() },
        Ok((code, _)) => Check { name: "central".into(), ok: false, detail: format!("HTTP {code}") },
        Err(e) => Check { name: "central".into(), ok: false, detail: format!("127.0.0.1:{port} no responde ({e})") },
    }
}

/// `services`: los servicios del puesto (los que hay que ver en marcha y qué HTTP comprobar).
pub fn wait(
    scm: Option<&dyn Scm>,
    services: &[&str],
    net: &NetSettings,
    data: &DataLayout,
    timeout: Duration,
    deep: bool,
) -> Result<Outcome, CtlError> {
    let t0 = Instant::now();
    let mut deep_missing = false;
    let mut last: Vec<Check>;
    loop {
        last = Vec::new();
        if let Some(scm) = scm {
            for s in services {
                let (ok, detail) = match scm.query(s) {
                    Ok(Some(info)) => (info.state == SvcState::Running, format!("{:?}", info.state)),
                    Ok(None) => (false, "no instalado".into()),
                    Err(e) => (false, e.message),
                };
                last.push(Check { name: (*s).to_string(), ok, detail });
            }
        }
        if services.contains(&BACKEND) {
            last.push(backend_check(net, data, deep, &mut deep_missing));
            if deep_missing && !last.last().is_some_and(|c| c.ok) {
                last.pop();
                last.push(backend_check(net, data, false, &mut deep_missing));
            }
        }
        if services.contains(&CENTRAL) {
            last.push(central_check(net));
        }
        if last.iter().all(|c| c.ok) {
            break;
        }
        if t0.elapsed() >= timeout {
            let bad: Vec<String> = last.iter().filter(|c| !c.ok).map(|c| format!("{}: {}", c.name, c.detail)).collect();
            let data = json!({"checks": checks_json(&last), "waited_s": t0.elapsed().as_secs()});
            return Err(CtlError::health(format!(
                "el puesto no está sano tras {} s: {}",
                timeout.as_secs(),
                bad.join("; ")
            ))
            .with_data(data));
        }
        std::thread::sleep(Duration::from_secs(1));
    }
    let data = json!({"checks": checks_json(&last), "waited_s": t0.elapsed().as_secs(), "deep": deep && !deep_missing,
                      "deep_unavailable": deep_missing});
    let mut text = format!("Puesto sano en {} s.", t0.elapsed().as_secs());
    if deep_missing {
        text.push_str(" (Comprobación profunda no disponible en esta versión: se usó /api/health.)");
    }
    Ok(Outcome::new(data, text))
}

fn checks_json(checks: &[Check]) -> Value {
    Value::Array(checks.iter().map(|c| json!({"name": c.name, "ok": c.ok, "detail": c.detail})).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    /// Servidor HTTP de una sola respuesta por conexión, para las pruebas.
    fn serve(responses: Vec<&'static str>) -> u16 {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for resp in responses {
                let Ok((mut s, _)) = l.accept() else { return };
                let mut buf = [0u8; 4096];
                let _ = s.read(&mut buf);
                let _ = s.write_all(resp.as_bytes());
            }
        });
        port
    }

    #[test]
    fn parses_plain_and_chunked_responses() {
        let (c, b) = parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok").unwrap();
        assert_eq!((c, b.as_slice()), (200, &b"ok"[..]));
        let (c, b) =
            parse_response(b"HTTP/1.1 503 X\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n2\r\nef\r\n0\r\n\r\n")
                .unwrap();
        assert_eq!((c, b.as_slice()), (503, &b"abcdef"[..]));
        assert!(parse_response(b"basura").is_err());
    }

    #[test]
    fn wait_succeeds_when_backend_is_up_and_fails_with_12_when_down() {
        let d = tempfile::tempdir().unwrap();
        let data = DataLayout::new(d.path());
        let body = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"ok\",\"engine\":{\"running\":true}}";
        let port = serve(vec![body]);
        let net = NetSettings::from_map([("VMS_HTTP_PORT".to_string(), port.to_string())].into_iter().collect());
        let out = wait(None, &[BACKEND], &net, &data, Duration::from_secs(5), false).unwrap();
        assert_eq!(out.data["checks"][0]["ok"], true);

        let down = "HTTP/1.1 200 OK\r\n\r\n{\"status\":\"down\"}";
        let port = serve(vec![down, down, down, down]);
        let net = NetSettings::from_map([("VMS_HTTP_PORT".to_string(), port.to_string())].into_iter().collect());
        let e = wait(None, &[BACKEND], &net, &data, Duration::from_secs(1), false).unwrap_err();
        assert_eq!(e.exit, vms_common::exit_codes::HEALTH_FAILED);
        assert!(e.message.contains("status=down"), "{}", e.message);
    }

    #[test]
    fn deep_falls_back_when_the_route_does_not_exist() {
        let d = tempfile::tempdir().unwrap();
        let data = DataLayout::new(d.path());
        std::fs::create_dir_all(data.secrets_dir()).unwrap();
        std::fs::write(data.secrets_dir().join("internal.token"), "tok").unwrap();
        let port = serve(vec!["HTTP/1.1 404 Not Found\r\n\r\n{}", "HTTP/1.1 200 OK\r\n\r\n{\"status\":\"degraded\"}"]);
        let net = NetSettings::from_map([("VMS_HTTP_PORT".to_string(), port.to_string())].into_iter().collect());
        let out = wait(None, &[BACKEND], &net, &data, Duration::from_secs(5), true).unwrap();
        assert_eq!(out.data["deep_unavailable"], true);
        assert!(out.text.contains("no disponible"));
    }
}
