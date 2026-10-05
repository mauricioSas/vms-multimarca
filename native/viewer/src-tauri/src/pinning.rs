//! Fijación del certificado de un servidor remoto (PLAN-V2 §2.3, prueba S5), como SSH:
//!
//! 1. La primera vez, el visor lee la huella SHA-256 del certificado con una conexión TLS propia (`probe`) y
//!    la persona la confirma («primer uso»). Se guarda en `viewer.json`.
//! 2. Antes de abrir una ventana contra ese servidor, se vuelve a leer: si cambió, la ventana no carga y
//!    muestra el aviso (`PinCheck::Changed`).
//! 3. Dentro de WebView2 (Windows), el certificado autofirmado del servidor se acepta solo si su huella es la
//!    guardada (`webview2_allows`, opción A de S5): cualquier otro se rechaza aunque la página ya estuviera
//!    abierta.
//!
//! La conexión TLS propia verifica la firma del intercambio con la clave del certificado: quien no tenga la
//! clave privada no puede presentar el certificado fijado.

use std::io::{self, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, WebPkiSupportedAlgorithms};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, ClientConnection, DigitallySignedStruct, SignatureScheme, StreamOwned};

use crate::servers::{origin_of, ServerUrl};

/// SHA-256 en hexadecimal en minúsculas, sin separadores (lo que se guarda).
pub fn fingerprint_hex(der: &[u8]) -> String {
    let d = ring::digest::digest(&ring::digest::SHA256, der);
    d.as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

/// Acepta `AB:CD:…`, `ab cd …` o `abcd…`; devuelve los 64 dígitos en minúsculas.
pub fn normalize_fingerprint(s: &str) -> Option<String> {
    let hex: String = s.chars().filter(|c| !matches!(c, ':' | ' ' | '-')).collect::<String>().to_ascii_lowercase();
    (hex.len() == 64 && hex.chars().all(|c| c.is_ascii_hexdigit())).then_some(hex)
}

/// Para mostrar: `AB:CD:EF:…` (como lo enseña Windows al ver un certificado).
pub fn display_fingerprint(hex: &str) -> String {
    hex.as_bytes().chunks(2).map(|c| String::from_utf8_lossy(c).to_ascii_uppercase()).collect::<Vec<_>>().join(":")
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PinCheck {
    Match,
    FirstUse { observed: String },
    Changed { expected: String, observed: String },
}

pub fn check(pin: Option<&str>, observed: &str) -> PinCheck {
    match pin.and_then(normalize_fingerprint) {
        None => PinCheck::FirstUse { observed: observed.to_string() },
        Some(p) if p == observed => PinCheck::Match,
        Some(p) => PinCheck::Changed { expected: p, observed: observed.to_string() },
    }
}

#[derive(Debug)]
pub enum ProbeError {
    /// No se pudo abrir la conexión TCP (servidor apagado, puerto cerrado, nombre que no resuelve).
    Unreachable(String),
    /// La conexión se abrió pero el intercambio TLS falló (no es HTTPS, protocolo, huella distinta…).
    Tls(String),
}

impl std::fmt::Display for ProbeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unreachable(m) => write!(f, "No se pudo conectar con el servidor: {m}"),
            Self::Tls(m) => write!(f, "La conexión segura falló: {m}"),
        }
    }
}

#[derive(Debug)]
struct PinVerifier {
    expected: Option<String>,
    seen: Mutex<Option<Vec<u8>>>,
    algs: WebPkiSupportedAlgorithms,
}

impl ServerCertVerifier for PinVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let fp = fingerprint_hex(end_entity.as_ref());
        if let Ok(mut seen) = self.seen.lock() {
            *seen = Some(end_entity.as_ref().to_vec());
        }
        match &self.expected {
            Some(e) if *e != fp => Err(rustls::Error::General("la huella del certificado no es la fijada".into())),
            _ => Ok(ServerCertVerified::assertion()),
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.algs)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.algs)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algs.supported_schemes()
    }
}

/// Conexión TLS abierta (con o sin huella exigida). Se usa para leer la huella y para el GET de salud.
pub struct TlsConn {
    pub stream: StreamOwned<ClientConnection, TcpStream>,
    pub sha256: String,
}

fn tcp_connect(url: &ServerUrl, timeout: Duration) -> Result<TcpStream, ProbeError> {
    let addrs: Vec<_> =
        (url.host.as_str(), url.port).to_socket_addrs().map_err(|e| ProbeError::Unreachable(e.to_string()))?.collect();
    let mut last = String::from("sin direcciones");
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, timeout) {
            Ok(s) => {
                let _ = s.set_read_timeout(Some(timeout));
                let _ = s.set_write_timeout(Some(timeout));
                let _ = s.set_nodelay(true);
                return Ok(s);
            }
            Err(e) => last = e.to_string(),
        }
    }
    Err(ProbeError::Unreachable(last))
}

/// Abre TCP + TLS contra `url`. Con `expected`, el intercambio falla si la huella no coincide.
pub fn connect(url: &ServerUrl, expected: Option<&str>, timeout: Duration) -> Result<TlsConn, ProbeError> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let verifier = Arc::new(PinVerifier {
        expected: expected.and_then(normalize_fingerprint),
        seen: Mutex::new(None),
        algs: provider.signature_verification_algorithms,
    });
    let config = ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| ProbeError::Tls(e.to_string()))?
        .dangerous()
        .with_custom_certificate_verifier(verifier.clone())
        .with_no_client_auth();
    let name = ServerName::try_from(url.host.clone()).map_err(|e| ProbeError::Tls(e.to_string()))?;
    let conn = ClientConnection::new(Arc::new(config), name).map_err(|e| ProbeError::Tls(e.to_string()))?;
    let sock = tcp_connect(url, timeout)?;
    let mut stream = StreamOwned::new(conn, sock);
    while stream.conn.is_handshaking() {
        stream.conn.complete_io(&mut stream.sock).map_err(|e| ProbeError::Tls(e.to_string()))?;
    }
    let der = verifier.seen.lock().ok().and_then(|s| s.clone()).ok_or(ProbeError::Tls("sin certificado".into()))?;
    Ok(TlsConn { stream, sha256: fingerprint_hex(&der) })
}

/// Lee la huella del certificado que presenta el servidor ahora mismo.
pub fn probe(url: &ServerUrl, timeout: Duration) -> Result<String, ProbeError> {
    let mut c = connect(url, None, timeout)?;
    c.stream.conn.send_close_notify();
    let _ = c.stream.flush();
    Ok(c.sha256)
}

impl Read for TlsConn {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self.stream.read(buf) {
            // un servidor que cierra sin close_notify (uvicorn lo hace) no es un error aquí
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => Ok(0),
            other => other,
        }
    }
}

impl Write for TlsConn {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.stream.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.stream.flush()
    }
}

/// PEM («-----BEGIN CERTIFICATE-----») → DER. Lo que entrega WebView2 en `ToPemEncoding`.
pub fn der_from_pem(pem: &str) -> Option<Vec<u8>> {
    let body: String = pem
        .lines()
        .map(str::trim)
        .skip_while(|l| !l.starts_with("-----BEGIN CERTIFICATE-----"))
        .skip(1)
        .take_while(|l| !l.starts_with("-----END"))
        .collect();
    base64_decode(&body)
}

fn base64_decode(s: &str) -> Option<Vec<u8>> {
    fn val(c: u8) -> Option<u32> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32)
    }
    let bytes: Vec<u8> = s.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    if bytes.is_empty() || !bytes.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for chunk in bytes.chunks(4) {
        let pad = chunk.iter().rev().take_while(|&&b| b == b'=').count();
        if pad > 2 {
            return None;
        }
        let mut n: u32 = 0;
        for (i, &c) in chunk.iter().enumerate() {
            let v = if i >= 4 - pad { 0 } else { val(c)? };
            n = (n << 6) | v;
        }
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Some(out)
}

/// Decisión de la opción A de S5: WebView2 avisa de un certificado que no puede verificar (autofirmado) para
/// `request_uri`; se acepta solo si es el de un servidor configurado y su huella es la guardada.
pub fn webview2_allows(request_uri: &str, pem: &str, pins: &[(String, String)]) -> bool {
    let Ok(url) = tauri::Url::parse(request_uri) else { return false };
    if url.scheme() != "https" {
        return false;
    }
    let Some(origin) = origin_of(&url) else { return false };
    let Some(der) = der_from_pem(pem) else { return false };
    let observed = fingerprint_hex(&der);
    pins.iter().any(|(o, fp)| *o == origin && normalize_fingerprint(fp).as_deref() == Some(observed.as_str()))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    use rustls::pki_types::PrivateKeyDer;
    use rustls::{ServerConfig, ServerConnection};

    use crate::servers::parse_server_url;

    pub struct TestCert {
        pub der: Vec<u8>,
        pub key_der: Vec<u8>,
        pub pem: String,
    }

    pub fn self_signed(names: &[&str]) -> TestCert {
        let names: Vec<String> = names.iter().map(|s| s.to_string()).collect();
        let ck = rcgen::generate_simple_self_signed(names).expect("certificado de prueba");
        TestCert { der: ck.cert.der().to_vec(), key_der: ck.signing_key.serialize_der(), pem: ck.cert.pem() }
    }

    /// Servidor TLS de prueba: atiende `conns` conexiones y responde `body` como HTTP.
    pub fn tls_server(cert: &TestCert, conns: usize, body: &'static str) -> u16 {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let cfg = ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(
                vec![CertificateDer::from(cert.der.clone())],
                PrivateKeyDer::try_from(cert.key_der.clone()).unwrap(),
            )
            .unwrap();
        let cfg = Arc::new(cfg);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            for _ in 0..conns {
                let Ok((sock, _)) = listener.accept() else { return };
                let cfg = cfg.clone();
                thread::spawn(move || {
                    let conn = ServerConnection::new(cfg).unwrap();
                    let mut s = StreamOwned::new(conn, sock);
                    let mut buf = [0u8; 2048];
                    let mut req = Vec::new();
                    while !req.windows(4).any(|w| w == b"\r\n\r\n") {
                        match s.read(&mut buf) {
                            Ok(0) | Err(_) => return,
                            Ok(n) => req.extend_from_slice(&buf[..n]),
                        }
                    }
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = s.write_all(resp.as_bytes());
                    s.conn.send_close_notify();
                    let _ = s.flush();
                });
            }
        });
        port
    }

    #[test]
    fn fingerprint_formats() {
        let fp = fingerprint_hex(b"hola");
        assert_eq!(fp.len(), 64);
        let shown = display_fingerprint(&fp);
        assert_eq!(shown.len(), 64 + 31);
        assert_eq!(normalize_fingerprint(&shown).as_deref(), Some(fp.as_str()));
        assert_eq!(normalize_fingerprint("zz"), None);
        assert_eq!(normalize_fingerprint(&"a".repeat(63)), None);
    }

    #[test]
    fn check_table() {
        let a = "a".repeat(64);
        let b = "b".repeat(64);
        assert_eq!(check(None, &a), PinCheck::FirstUse { observed: a.clone() });
        assert_eq!(check(Some(&a.to_uppercase()), &a), PinCheck::Match);
        assert_eq!(check(Some(&a), &b), PinCheck::Changed { expected: a.clone(), observed: b.clone() });
        assert_eq!(check(Some("basura"), &a), PinCheck::FirstUse { observed: a });
    }

    #[test]
    fn probe_reads_the_real_certificate_and_detects_a_change() {
        let cert_a = self_signed(&["127.0.0.1", "localhost"]);
        let cert_b = self_signed(&["127.0.0.1", "localhost"]);
        let port_a = tls_server(&cert_a, 2, "{}");
        let url_a = parse_server_url(&format!("https://127.0.0.1:{port_a}")).unwrap();
        let fp_a = probe(&url_a, Duration::from_secs(5)).expect("huella");
        assert_eq!(fp_a, fingerprint_hex(&cert_a.der));
        assert_eq!(check(Some(&fp_a), &fp_a), PinCheck::Match);

        // el servidor cambia de certificado (reinstalado, o alguien se hace pasar por él)
        let port_b = tls_server(&cert_b, 1, "{}");
        let url_b = parse_server_url(&format!("https://127.0.0.1:{port_b}")).unwrap();
        let fp_b = probe(&url_b, Duration::from_secs(5)).unwrap();
        assert!(matches!(check(Some(&fp_a), &fp_b), PinCheck::Changed { .. }));

        // con la huella exigida, la conexión ni se completa
        let err = connect(&url_a, Some(&"0".repeat(64)), Duration::from_secs(5)).err().expect("rechazo");
        assert!(matches!(err, ProbeError::Tls(_)), "{err}");
    }

    #[test]
    fn probe_of_a_closed_port_is_unreachable() {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let url = parse_server_url(&format!("https://127.0.0.1:{port}")).unwrap();
        assert!(matches!(probe(&url, Duration::from_secs(2)), Err(ProbeError::Unreachable(_))));
    }

    #[test]
    fn plain_http_server_is_not_mistaken_for_tls() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let _ = s.write_all(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n");
            }
        });
        let url = parse_server_url(&format!("https://127.0.0.1:{port}")).unwrap();
        assert!(matches!(probe(&url, Duration::from_secs(2)), Err(ProbeError::Tls(_))));
    }

    #[test]
    fn pem_to_der_matches_rcgen() {
        let c = self_signed(&["srv"]);
        assert_eq!(der_from_pem(&c.pem).unwrap(), c.der);
        // WebView2 entrega el PEM con CRLF
        assert_eq!(der_from_pem(&c.pem.replace('\n', "\r\n")).unwrap(), c.der);
        assert_eq!(der_from_pem("nada"), None);
        assert_eq!(base64_decode("aG9sYQ=="), Some(b"hola".to_vec()));
        assert_eq!(base64_decode("aG9sYSE="), Some(b"hola!".to_vec()));
        assert_eq!(base64_decode("aG9s"), Some(b"hol".to_vec()));
        assert_eq!(base64_decode("a"), None);
    }

    #[test]
    fn webview2_decision_only_for_the_pinned_server() {
        let a = self_signed(&["central"]);
        let b = self_signed(&["central"]);
        let pins = vec![("https://central:8643".to_string(), fingerprint_hex(&a.der))];
        assert!(webview2_allows("https://central:8643/wall/1", &a.pem, &pins));
        assert!(webview2_allows("https://CENTRAL:8643/api/events", &a.pem, &pins));
        assert!(!webview2_allows("https://central:8643/", &b.pem, &pins), "certificado cambiado");
        assert!(!webview2_allows("https://central:9999/", &a.pem, &pins), "otro puerto");
        assert!(!webview2_allows("https://otro:8643/", &a.pem, &pins), "otro servidor");
        assert!(!webview2_allows("http://central:8643/", &a.pem, &pins));
        assert!(!webview2_allows("basura", &a.pem, &pins));
        assert!(!webview2_allows("https://central:8643/", "sin pem", &pins));
    }
}
