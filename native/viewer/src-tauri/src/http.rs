//! Cliente HTTP/1.1 mínimo para `GET /api/health` (bandeja y «Conectando…»). Sin dependencias: para el
//! servidor local va en claro por 127.0.0.1; para uno remoto, por TLS con la huella fijada (`pinning`).

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use crate::pinning;
use crate::servers::ServerUrl;

const MAX_BODY: usize = 1 << 20;

#[derive(Debug, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

#[derive(Debug)]
pub enum HttpError {
    Unreachable(String),
    Pin(String),
    Protocol(String),
}

impl std::fmt::Display for HttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unreachable(m) => write!(f, "sin conexión: {m}"),
            Self::Pin(m) => write!(f, "certificado no válido: {m}"),
            Self::Protocol(m) => write!(f, "respuesta no válida: {m}"),
        }
    }
}

fn request_bytes(url: &ServerUrl, path: &str) -> Vec<u8> {
    format!(
        "GET {path} HTTP/1.1\r\nHost: {}\r\nAccept: application/json\r\nUser-Agent: VMS-visor/{}\r\nConnection: close\r\n\r\n",
        url.authority(),
        env!("CARGO_PKG_VERSION")
    )
    .into_bytes()
}

fn read_all(r: &mut impl Read) -> Result<Vec<u8>, HttpError> {
    let mut out = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        match r.read(&mut buf) {
            Ok(0) => return Ok(out),
            Ok(n) => {
                out.extend_from_slice(&buf[..n]);
                if out.len() > MAX_BODY + 16 * 1024 {
                    return Err(HttpError::Protocol("respuesta demasiado grande".into()));
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => {
                // con Connection: close, un corte después de llegar algo se trata como fin
                if out.is_empty() {
                    return Err(HttpError::Unreachable(e.to_string()));
                }
                return Ok(out);
            }
        }
    }
}

/// Analiza una respuesta HTTP/1.1 completa (cabeceras + cuerpo con Content-Length, chunked o hasta el cierre).
pub fn parse_response(raw: &[u8]) -> Result<Response, HttpError> {
    let end = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| HttpError::Protocol("cabeceras incompletas".into()))?;
    let head = std::str::from_utf8(&raw[..end]).map_err(|_| HttpError::Protocol("cabeceras no UTF-8".into()))?;
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap_or("");
    let mut parts = status_line.splitn(3, ' ');
    let version = parts.next().unwrap_or("");
    if !version.starts_with("HTTP/1.") {
        return Err(HttpError::Protocol(format!("línea de estado inesperada: {status_line:.40}")));
    }
    let status: u16 =
        parts.next().and_then(|s| s.parse().ok()).ok_or_else(|| HttpError::Protocol("sin código de estado".into()))?;
    let mut length: Option<usize> = None;
    let mut chunked = false;
    for line in lines {
        let Some((k, v)) = line.split_once(':') else { continue };
        let (k, v) = (k.trim().to_ascii_lowercase(), v.trim());
        if k == "content-length" {
            length = v.parse().ok();
        } else if k == "transfer-encoding" && v.to_ascii_lowercase().contains("chunked") {
            chunked = true;
        }
    }
    let rest = &raw[end + 4..];
    let body = if chunked {
        decode_chunked(rest)?
    } else if let Some(n) = length {
        if rest.len() < n {
            return Err(HttpError::Protocol("cuerpo incompleto".into()));
        }
        rest[..n].to_vec()
    } else {
        rest.to_vec()
    };
    if body.len() > MAX_BODY {
        return Err(HttpError::Protocol("respuesta demasiado grande".into()));
    }
    Ok(Response { status, body })
}

fn decode_chunked(mut data: &[u8]) -> Result<Vec<u8>, HttpError> {
    let mut out = Vec::new();
    loop {
        let line_end =
            data.windows(2).position(|w| w == b"\r\n").ok_or_else(|| HttpError::Protocol("trozo incompleto".into()))?;
        let size_txt = std::str::from_utf8(&data[..line_end]).map_err(|_| HttpError::Protocol("trozo".into()))?;
        let size = usize::from_str_radix(size_txt.split(';').next().unwrap_or("").trim(), 16)
            .map_err(|_| HttpError::Protocol("tamaño de trozo".into()))?;
        data = &data[line_end + 2..];
        if size == 0 {
            return Ok(out);
        }
        if data.len() < size + 2 || out.len() + size > MAX_BODY {
            return Err(HttpError::Protocol("trozo incompleto".into()));
        }
        out.extend_from_slice(&data[..size]);
        data = &data[size + 2..];
    }
}

/// `GET path` contra el servidor. Para https exige `pin` (sin huella no se habla con un servidor remoto).
pub fn get(url: &ServerUrl, path: &str, pin: Option<&str>, timeout: Duration) -> Result<Response, HttpError> {
    let req = request_bytes(url, path);
    let raw = if url.https {
        let pin = pin.ok_or_else(|| HttpError::Pin("el servidor no tiene huella guardada".into()))?;
        let mut conn = pinning::connect(url, Some(pin), timeout).map_err(|e| match e {
            pinning::ProbeError::Unreachable(m) => HttpError::Unreachable(m),
            pinning::ProbeError::Tls(m) => HttpError::Pin(m),
        })?;
        conn.write_all(&req).map_err(|e| HttpError::Unreachable(e.to_string()))?;
        conn.flush().map_err(|e| HttpError::Unreachable(e.to_string()))?;
        read_all(&mut conn)?
    } else {
        let addr = (url.host.as_str(), url.port)
            .to_socket_addrs()
            .map_err(|e| HttpError::Unreachable(e.to_string()))?
            .next()
            .ok_or_else(|| HttpError::Unreachable("sin dirección".into()))?;
        let mut s = TcpStream::connect_timeout(&addr, timeout).map_err(|e| HttpError::Unreachable(e.to_string()))?;
        let _ = s.set_read_timeout(Some(timeout));
        let _ = s.set_write_timeout(Some(timeout));
        s.write_all(&req).map_err(|e| HttpError::Unreachable(e.to_string()))?;
        read_all(&mut s)?
    };
    parse_response(&raw)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pinning::tests::{self_signed, tls_server};
    use crate::servers::parse_server_url;
    use std::net::TcpListener;
    use std::thread;

    fn serve_once(reply: &'static [u8]) -> u16 {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                let mut buf = [0u8; 1024];
                let _ = s.read(&mut buf);
                let _ = s.write_all(reply);
            }
        });
        port
    }

    #[test]
    fn parses_content_length_chunked_and_close() {
        let r = parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}extra").unwrap();
        assert_eq!(r, Response { status: 200, body: b"{}".to_vec() });
        let r = parse_response(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2;x=y\r\nde\r\n0\r\n\r\n",
        )
        .unwrap();
        assert_eq!(r.body, b"abcde");
        let r = parse_response(b"HTTP/1.0 503 Service Unavailable\r\n\r\nsin motor").unwrap();
        assert_eq!((r.status, r.body.as_slice()), (503, &b"sin motor"[..]));
        assert!(parse_response(b"SSH-2.0-OpenSSH\r\n\r\n").is_err());
        assert!(parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\ncorto").is_err());
    }

    #[test]
    fn plain_get_against_local_server() {
        let port = serve_once(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}",
        );
        let url = parse_server_url(&format!("http://127.0.0.1:{port}")).unwrap();
        let r = get(&url, "/api/health", None, Duration::from_secs(3)).unwrap();
        assert_eq!(r.status, 200);
        assert_eq!(r.body, br#"{"status":"ok"}"#);
    }

    #[test]
    fn closed_port_is_unreachable() {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let url = parse_server_url(&format!("http://127.0.0.1:{port}")).unwrap();
        assert!(matches!(get(&url, "/", None, Duration::from_secs(2)), Err(HttpError::Unreachable(_))));
    }

    #[test]
    fn https_requires_and_enforces_the_pin() {
        let cert = self_signed(&["127.0.0.1"]);
        let port = tls_server(&cert, 2, r#"{"status":"degraded"}"#);
        let url = parse_server_url(&format!("https://127.0.0.1:{port}")).unwrap();
        assert!(matches!(get(&url, "/api/health", None, Duration::from_secs(3)), Err(HttpError::Pin(_))));
        let fp = crate::pinning::fingerprint_hex(&cert.der);
        let r = get(&url, "/api/health", Some(&fp), Duration::from_secs(3)).unwrap();
        assert_eq!(r.body, br#"{"status":"degraded"}"#);
        let wrong = "f".repeat(64);
        assert!(matches!(get(&url, "/api/health", Some(&wrong), Duration::from_secs(3)), Err(HttpError::Pin(_))));
    }
}
