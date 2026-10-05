//! Servidores a los que se conecta el visor y qué puede abrir cada ventana (PLAN-V2 §2.3).
//!
//! - `http://` solo para el propio equipo (127.0.0.1, ::1 o localhost). Un servidor remoto tiene que ir por
//!   `https://` y con la huella SHA-256 de su certificado fijada (`pinning`).
//! - Las ventanas solo navegan a las páginas locales del visor y a los servidores configurados.

use tauri::Url;

pub const LOCAL_SERVER: &str = "local";
pub const DEFAULT_LOCAL_URL: &str = "http://127.0.0.1:8600";

/// URL de un servidor ya normalizada a su origen (`esquema://host:puerto`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServerUrl {
    pub https: bool,
    pub host: String,
    pub port: u16,
}

impl ServerUrl {
    pub fn origin(&self) -> String {
        let scheme = if self.https { "https" } else { "http" };
        if self.host.contains(':') {
            format!("{scheme}://[{}]:{}", self.host, self.port)
        } else {
            format!("{scheme}://{}:{}", self.host, self.port)
        }
    }

    /// `origin` + ruta (que empieza por `/`).
    pub fn join(&self, path: &str) -> Result<Url, String> {
        if !path.starts_with('/') || path.starts_with("//") || path.contains('\\') {
            return Err(format!("ruta no válida: {path}"));
        }
        Url::parse(&format!("{}{}", self.origin(), path)).map_err(|e| e.to_string())
    }

    /// `host:puerto` sin corchetes (para la conexión TCP y el nombre del certificado).
    pub fn authority(&self) -> String {
        if self.host.contains(':') {
            format!("[{}]:{}", self.host, self.port)
        } else {
            format!("{}:{}", self.host, self.port)
        }
    }

    pub fn is_loopback(&self) -> bool {
        is_loopback_host(&self.host)
    }
}

fn is_loopback_host(host: &str) -> bool {
    let h = host.trim_start_matches('[').trim_end_matches(']');
    h.eq_ignore_ascii_case("localhost") || h.parse::<std::net::IpAddr>().map(|ip| ip.is_loopback()).unwrap_or(false)
}

/// Valida y normaliza la URL de un servidor tal como la escribe una persona.
pub fn parse_server_url(input: &str) -> Result<ServerUrl, String> {
    let text = input.trim();
    if text.is_empty() {
        return Err("Escribe la dirección del servidor, por ejemplo https://192.168.1.20:8643".into());
    }
    let with_scheme = if text.contains("://") { text.to_string() } else { format!("https://{text}") };
    let url = Url::parse(&with_scheme).map_err(|_| format!("«{text}» no es una dirección válida"))?;
    let https = match url.scheme() {
        "https" => true,
        "http" => false,
        other => return Err(format!("El esquema «{other}» no está admitido: usa https://")),
    };
    if !url.username().is_empty() || url.password().is_some() {
        return Err("La dirección no puede llevar usuario ni contraseña".into());
    }
    if url.query().is_some() || url.fragment().is_some() || !matches!(url.path(), "" | "/") {
        return Err("Escribe solo la dirección del servidor, sin ruta (por ejemplo https://servidor:8643)".into());
    }
    let host = match url.host() {
        Some(url::Host::Domain(d)) => d.to_ascii_lowercase(),
        Some(url::Host::Ipv4(ip)) => ip.to_string(),
        Some(url::Host::Ipv6(ip)) => ip.to_string(),
        None => return Err("Falta el nombre o la IP del servidor".into()),
    };
    let port = url.port_or_known_default().ok_or("Falta el puerto")?;
    let out = ServerUrl { https, host, port };
    if !https && !out.is_loopback() {
        return Err("Un servidor de otro equipo tiene que usar https:// (con su certificado fijado)".into());
    }
    Ok(out)
}

/// Origen de las páginas locales del visor en esta plataforma.
pub fn local_origin() -> &'static str {
    if cfg!(windows) {
        "http://tauri.localhost"
    } else {
        "tauri://localhost"
    }
}

/// URL de una página local del visor (`ui/<pagina>`), con consulta opcional.
pub fn local_page(page: &str, query: &[(&str, &str)]) -> Url {
    let mut url =
        Url::parse(&format!("{}/{}", local_origin(), page.trim_start_matches('/'))).expect("URL local válida");
    if !query.is_empty() {
        let mut q = url.query_pairs_mut();
        for (k, v) in query {
            q.append_pair(k, v);
        }
    }
    url
}

pub fn is_local_url(url: &Url) -> bool {
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        _ => false,
    }
}

/// Origen `esquema://host:puerto` de una URL http(s) (con el puerto explícito, como `ServerUrl::origin`).
pub fn origin_of(url: &Url) -> Option<String> {
    let https = match url.scheme() {
        "https" => true,
        "http" => false,
        _ => return None,
    };
    let host = match url.host()? {
        url::Host::Domain(d) => d.to_ascii_lowercase(),
        url::Host::Ipv4(ip) => ip.to_string(),
        url::Host::Ipv6(ip) => ip.to_string(),
    };
    Some(ServerUrl { https, host, port: url.port_or_known_default()? }.origin())
}

/// ¿Puede una ventana del visor navegar a `url`? Solo páginas locales, servidores configurados y
/// `about:blank` (lo usa el propio WebView al crearse).
pub fn navigation_allowed(url: &Url, allowed_origins: &[String]) -> bool {
    if url.as_str() == "about:blank" || is_local_url(url) {
        return true;
    }
    match origin_of(url) {
        Some(o) => allowed_origins.iter().any(|a| a == &o),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_http_and_remote_https() {
        let l = parse_server_url("http://127.0.0.1:8600").unwrap();
        assert_eq!(l.origin(), "http://127.0.0.1:8600");
        assert!(l.is_loopback());
        let r = parse_server_url(" HTTPS://Servidor-Central:8643/ ").unwrap();
        assert_eq!(r.origin(), "https://servidor-central:8643");
        assert_eq!(r.authority(), "servidor-central:8643");
        let no_scheme = parse_server_url("192.168.1.20:8643").unwrap();
        assert!(no_scheme.https);
        let v6 = parse_server_url("https://[fd00::20]:8643").unwrap();
        assert_eq!(v6.origin(), "https://[fd00::20]:8643");
        assert_eq!(parse_server_url("https://srv").unwrap().port, 443);
    }

    #[test]
    fn rejects_what_would_bypass_pinning_or_confuse() {
        for bad in [
            "",
            "http://192.168.1.20:8600",
            "ftp://x",
            "https://user:pw@srv:8643",
            "https://srv:8643/wall/1",
            "https://srv:8643/?a=1",
            "file:///C:/x",
            "javascript:alert(1)",
        ] {
            assert!(parse_server_url(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn join_only_internal_paths() {
        let s = parse_server_url("http://127.0.0.1:8600").unwrap();
        assert_eq!(s.join("/wall/2").unwrap().as_str(), "http://127.0.0.1:8600/wall/2");
        assert!(s.join("//evil.example/x").is_err());
        assert!(s.join("wall/2").is_err());
        assert!(s.join("/\\evil").is_err());
    }

    #[test]
    fn navigation_is_limited_to_configured_servers() {
        let allowed = vec!["http://127.0.0.1:8600".to_string(), "https://central:8643".to_string()];
        let ok = |u: &str| navigation_allowed(&Url::parse(u).unwrap(), &allowed);
        assert!(ok("http://127.0.0.1:8600/wall/1"));
        assert!(ok("https://central:8643/"));
        assert!(ok("https://CENTRAL:8643/login?next=/"));
        assert!(ok("tauri://localhost/conectando.html"));
        assert!(ok("http://tauri.localhost/servidores.html"));
        assert!(ok("about:blank"));
        assert!(!ok("http://127.0.0.1:8601/"));
        assert!(!ok("https://central:8644/"));
        assert!(!ok("http://central:8643/"));
        assert!(!ok("https://evil.example/"));
        assert!(!ok("file:///C:/Windows/win.ini"));
        assert!(!ok("data:text/html,hola"));
        assert!(!ok("http://tauri.localhost.evil.example/"));
    }

    #[test]
    fn local_pages() {
        let u = local_page("conectando.html", &[("ventana", "muro-1")]);
        assert!(is_local_url(&u));
        assert_eq!(u.query(), Some("ventana=muro-1"));
        assert!(!is_local_url(&Url::parse("http://127.0.0.1:8600/").unwrap()));
    }
}
